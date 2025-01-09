from flask import Flask, render_template, request, flash, redirect, url_for, jsonify
import feedparser
import requests
from models import db, RSSFeed, Config, PostedItem, CustomPost
from datetime import datetime, timedelta
import os
import logging
from logging.handlers import RotatingFileHandler
from dotenv import load_dotenv
from apscheduler.schedulers.background import BackgroundScheduler
from apscheduler.triggers.interval import IntervalTrigger
from flask_migrate import Migrate
import traceback
import re
from bs4 import BeautifulSoup
import html

load_dotenv()

# Configure logging
logging.basicConfig(level=logging.INFO)
logger = logging.getLogger('autoposter')
handler = RotatingFileHandler('autoposter.log', maxBytes=10000000, backupCount=5)
handler.setFormatter(logging.Formatter(
    '[%(asctime)s] %(levelname)s in %(module)s: %(message)s'
))
logger.addHandler(handler)

app = Flask(__name__)
app.config['SECRET_KEY'] = os.getenv('FLASK_SECRET_KEY', 'default-secret-key')
app.config['SQLALCHEMY_DATABASE_URI'] = 'sqlite:///autoposter.db'
app.config['SQLALCHEMY_TRACK_MODIFICATIONS'] = False

db.init_app(app)
migrate = Migrate(app, db)

# Initialize scheduler with error handling
scheduler = BackgroundScheduler({
    'apscheduler.job_defaults.max_instances': 1,
    'apscheduler.job_defaults.misfire_grace_time': 15*60
})
scheduler.start()

def get_config(key, default=None):
    try:
        config = Config.query.filter_by(key=key).first()
        return config.value if config else default
    except Exception as e:
        logger.error(f"Error getting config {key}: {str(e)}")
        return default

def set_config(key, value):
    try:
        config = Config.query.filter_by(key=key).first()
        if not config:
            config = Config(key=key)
        config.value = value
        db.session.add(config)
        db.session.commit()
        logger.info(f"Config {key} updated successfully")
    except Exception as e:
        logger.error(f"Error setting config {key}: {str(e)}")
        db.session.rollback()
        raise

def validate_feed_url(url):
    """Validate RSS feed URL and return feed data if valid"""
    try:
        feed = feedparser.parse(url)
        if feed.bozo:
            logger.warning(f"Invalid RSS feed at {url}: {feed.bozo_exception}")
            return None, "Invalid RSS feed format"
        if not feed.entries:
            return None, "No entries found in feed"
        return feed, None
    except Exception as e:
        logger.error(f"Error parsing feed {url}: {str(e)}")
        return None, f"Error parsing feed: {str(e)}"

def check_posting_conditions(feed):
    """Check if posting conditions are met"""
    current_hour = datetime.utcnow().hour
    
    # Check daily reset
    today = datetime.utcnow().date()
    last_reset = feed.last_post_count_reset.date() if feed.last_post_count_reset else None
    if last_reset != today:
        feed.posts_today = 0
        feed.last_post_count_reset = datetime.utcnow()
        db.session.commit()
        logger.info(f"Reset daily post count for feed {feed.id}")
    
    conditions = {
        'within_limit': feed.posts_today < feed.max_posts_per_day,
        'within_window': feed.posting_window_start <= current_hour <= feed.posting_window_end,
        'interval_ok': True
    }
    
    if feed.last_post_time:
        time_since_last = (datetime.utcnow() - feed.last_post_time).total_seconds()
        conditions['interval_ok'] = time_since_last >= feed.min_interval_between_posts
    
    return conditions

def post_to_facebook_auto(feed_id):
    with app.app_context():
        try:
            feed = RSSFeed.query.get(feed_id)
            if not feed:
                logger.error(f"Feed {feed_id} not found")
                return

            conditions = check_posting_conditions(feed)
            if not all(conditions.values()):
                logger.info(f"Posting conditions not met for feed {feed_id}: {conditions}")
                return

            access_token = get_config('facebook_access_token')
            if not access_token:
                logger.error(f"No Facebook access token configured for feed {feed_id}")
                return

            # Parse feed
            feed_data, error = validate_feed_url(feed.url)
            if error:
                logger.error(f"Feed validation error for {feed_id}: {error}")
                return

            # Get already posted items
            posted_guids = set(p.item_guid for p in PostedItem.query.filter_by(feed_id=feed_id).all())
            
            # Sort entries
            entries = feed_data.entries
            if feed.post_order == 'newest':
                entries = sorted(entries, key=lambda x: x.get('published_parsed', 0), reverse=True)
            else:
                entries = sorted(entries, key=lambda x: x.get('published_parsed', 0))

            # Find suitable item to post
            for item in entries:
                if process_feed_item(feed, item, posted_guids, access_token):
                    break

        except Exception as e:
            logger.error(f"Error in auto posting for feed {feed_id}: {str(e)}\n{traceback.format_exc()}")

def extract_media_from_content(content, feed):
    """Extract media URLs from content based on feed settings"""
    media = {'videos': [], 'images': []}
    
    if feed.extract_youtube and 'youtube.com' in content:
        # Extract YouTube URLs using regex
        youtube_pattern = r'(?:https?:\/\/)?(?:www\.)?(?:youtube\.com\/watch\?v=|youtu\.be\/)([^"\s&]+)'
        youtube_matches = re.findall(youtube_pattern, content)
        media['videos'].extend([f'https://youtube.com/watch?v={vid_id}' for vid_id in youtube_matches])
    
    if feed.extract_twitter and 'twitter.com' in content:
        # Extract Twitter video URLs
        twitter_pattern = r'https?:\/\/(?:www\.)?twitter\.com\/\w+\/status\/\d+'
        twitter_matches = re.findall(twitter_pattern, content)
        media['videos'].extend(twitter_matches)
    
    if feed.extract_tiktok and 'tiktok.com' in content:
        # Extract TikTok video URLs
        tiktok_pattern = r'https?:\/\/(?:www\.)?tiktok\.com\/@[\w.-]+\/video\/\d+'
        tiktok_matches = re.findall(tiktok_pattern, content)
        media['videos'].extend(tiktok_matches)
    
    if feed.extract_images:
        # Extract image URLs using regex
        img_pattern = r'https?:\/\/[^"\s>]+?\.(?:jpg|jpeg|gif|png|webp)(?:\?[^"\s>]*)?'
        img_matches = re.findall(img_pattern, content)
        media['images'].extend(img_matches)
    
    return media

def post_to_facebook_api(access_token, message, media=None):
    """Post message and media to Facebook API"""
    try:
        if not media:
            # Text-only post
            response = requests.post(
                'https://graph.facebook.com/v17.0/me/feed',
                params={
                    'access_token': access_token,
                    'message': message
                }
            )
        elif media.get('videos') and (not media.get('images') or feed.prefer_video):
            # Video post (using first video found)
            response = requests.post(
                'https://graph.facebook.com/v17.0/me/feed',
                params={
                    'access_token': access_token,
                    'message': message,
                    'link': media['videos'][0]  # Facebook will automatically embed the video
                }
            )
        elif media.get('images'):
            # Photo post (using first image found)
            response = requests.post(
                'https://graph.facebook.com/v17.0/me/photos',
                params={
                    'access_token': access_token,
                    'message': message,
                    'url': media['images'][0]
                }
            )
        
        response.raise_for_status()
        return response.json()
    except requests.exceptions.RequestException as e:
        logger.error(f"Facebook API error: {str(e)}")
        return None

def process_feed_item(feed, item, posted_guids, access_token):
    """Process a single feed item for posting"""
    try:
        guid = item.get('id', item.get('link', ''))
        if guid in posted_guids:
            return False

        # Get content parts
        title = item.get('title', '') if feed.include_title else ''
        description = item.get('description', '') if feed.include_description else ''
        link = item.get('link', '') if feed.include_link else ''
        
        # Content validation
        content_length = len(title + description)
        if not (feed.min_content_length <= content_length <= feed.max_content_length):
            return False

        # Keyword filtering
        content_lower = (title + description).lower()
        if feed.required_keywords:
            required_words = [w.strip().lower() for w in feed.required_keywords.split(',')]
            if not all(word in content_lower for word in required_words):
                return False

        if feed.excluded_keywords:
            excluded_words = [w.strip().lower() for w in feed.excluded_keywords.split(',')]
            if any(word in content_lower for word in excluded_words):
                return False

        # Extract media if enabled
        media = None
        if feed.include_media:
            content_for_media = f"{description} {link}"
            media = extract_media_from_content(content_for_media, feed)
            
        # Build message
        message = build_post_message(feed, title, description, link)
        
        # Post to Facebook with media if available
        response = post_to_facebook_api(access_token, message, media)
        if response:
            save_posted_item(feed, guid, response.get('id'))
            logger.info(f"Successfully posted item {guid} for feed {feed.id}")
            return True

    except Exception as e:
        logger.error(f"Error processing feed item: {str(e)}\n{traceback.format_exc()}")
    
    return False

def build_post_message(feed, title, description, link):
    """Build the message to post to Facebook"""
    message_parts = []
    
    # Add custom prefix if set
    if feed.custom_prefix:
        message_parts.append(feed.custom_prefix)
    
    # Add title if enabled
    if feed.include_title and title:
        # Decode HTML entities and strip tags
        clean_title = BeautifulSoup(html.unescape(title), 'html.parser').get_text().strip()
        message_parts.append(clean_title)
    
    # Add description if enabled
    if feed.include_description and description:
        # Decode HTML entities and strip tags
        clean_description = BeautifulSoup(html.unescape(description), 'html.parser').get_text().strip()
        message_parts.append(clean_description)
    
    # Add link if enabled
    if feed.include_link and link:
        message_parts.append(link)
    
    # Add custom suffix if set
    if feed.custom_suffix:
        message_parts.append(feed.custom_suffix)
    
    # Join all parts with double newlines
    return '\n\n'.join(part for part in message_parts if part.strip())

def save_posted_item(feed, guid, post_id):
    """Save posted item to database"""
    try:
        posted = PostedItem(
            feed_id=feed.id,
            item_guid=guid,
            facebook_post_id=post_id
        )
        db.session.add(posted)
        feed.posts_today += 1
        feed.last_post_time = datetime.utcnow()
        db.session.commit()
    except Exception as e:
        logger.error(f"Error saving posted item: {str(e)}")
        db.session.rollback()
        raise

@app.route('/')
def index():
    try:
        feeds = RSSFeed.query.all()
        access_token = get_config('facebook_access_token')
        append_text = get_config('append_text', '')
        post_delay = get_config('post_delay', '3600')
        
        # Get status for each feed
        feed_statuses = {}
        for feed in feeds:
            conditions = check_posting_conditions(feed)
            feed_statuses[feed.id] = {
                'can_post': all(conditions.values()),
                'conditions': conditions,
                'is_autoposting': bool(scheduler.get_job(f'feed_{feed.id}'))
            }
        
        return render_template('index.html', 
                             feeds=feeds,
                             access_token=access_token,
                             append_text=append_text,
                             post_delay=post_delay,
                             scheduler=scheduler,
                             feed_statuses=feed_statuses)
    except Exception as e:
        logger.error(f"Error in index route: {str(e)}\n{traceback.format_exc()}")
        flash("An error occurred while loading the dashboard", "error")
        return render_template('index.html', feeds=[], scheduler=scheduler)

@app.route('/feed/add', methods=['POST'])
def add_feed():
    try:
        url = request.form.get('url')
        name = request.form.get('name')
        
        if not url:
            flash('Feed URL is required', 'error')
            return redirect(url_for('index'))
        
        feed_data, error = validate_feed_url(url)
        if error:
            flash(error, 'error')
            return redirect(url_for('index'))
        
        # Check if feed already exists
        existing_feed = RSSFeed.query.filter_by(url=url).first()
        if existing_feed:
            flash('This feed has already been added', 'warning')
            return redirect(url_for('index'))
        
        feed = RSSFeed(
            url=url,
            name=name or feed_data.feed.get('title', url),
            last_fetch=datetime.utcnow(),
            post_order='newest',  # Default values for new feeds
            max_posts_per_day=10,
            posting_window_start=0,
            posting_window_end=23,
            posts_today=0,
            min_interval_between_posts=300,
            min_content_length=0,
            max_content_length=5000,
            include_title=True,
            include_description=True,
            include_link=True
        )
        db.session.add(feed)
        db.session.commit()
        
        logger.info(f"Added new feed: {feed.name} ({feed.url})")
        flash('Feed added successfully', 'success')
        
    except Exception as e:
        logger.error(f"Error adding feed: {str(e)}\n{traceback.format_exc()}")
        db.session.rollback()
        flash('Error adding feed. Please try again.', 'error')
    
    return redirect(url_for('index'))

@app.route('/feed/<int:feed_id>/delete', methods=['POST'])
def delete_feed(feed_id):
    try:
        feed = RSSFeed.query.get_or_404(feed_id)
        
        # Stop any running auto-post job
        job_id = f'feed_{feed_id}'
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
            logger.info(f"Stopped auto-posting job for feed {feed_id}")
        
        # Delete associated posted items
        PostedItem.query.filter_by(feed_id=feed_id).delete()
        
        # Delete the feed
        name = feed.name
        url = feed.url
        db.session.delete(feed)
        db.session.commit()
        
        logger.info(f"Deleted feed: {name} ({url})")
        flash('Feed deleted successfully', 'success')
        
    except Exception as e:
        logger.error(f"Error deleting feed {feed_id}: {str(e)}\n{traceback.format_exc()}")
        db.session.rollback()
        flash('Error deleting feed. Please try again.', 'error')
    
    return redirect(url_for('index'))

@app.route('/config/update', methods=['POST'])
def update_config():
    try:
        access_token = request.form.get('access_token')
        append_text = request.form.get('append_text')
        post_delay = request.form.get('post_delay')
        
        if access_token:
            # Validate access token
            try:
                response = requests.get('https://graph.facebook.com/v17.0/me', 
                                     params={'access_token': access_token})
                response.raise_for_status()
                set_config('facebook_access_token', access_token)
                logger.info("Facebook access token updated and validated")
            except requests.exceptions.RequestException as e:
                logger.error(f"Invalid Facebook access token: {str(e)}")
                flash('Invalid Facebook access token', 'error')
                return redirect(url_for('index'))
        
        if append_text is not None:
            set_config('append_text', append_text.strip())
        
        if post_delay:
            try:
                delay = int(post_delay)
                if delay < 300:
                    flash('Post delay must be at least 300 seconds', 'warning')
                    delay = 300
                set_config('post_delay', str(delay))
                
                # Update all active jobs with new delay
                for job in scheduler.get_jobs():
                    if job.id.startswith('feed_'):
                        job.reschedule(trigger=IntervalTrigger(seconds=delay))
                logger.info(f"Updated post delay to {delay} seconds")
            except ValueError:
                flash('Invalid post delay value', 'error')
                return redirect(url_for('index'))
        
        flash('Configuration updated successfully', 'success')
        
    except Exception as e:
        logger.error(f"Error updating config: {str(e)}\n{traceback.format_exc()}")
        flash('Error updating configuration', 'error')
    
    return redirect(url_for('index'))

@app.route('/feed/<int:feed_id>/autopost', methods=['POST'])
def toggle_autopost(feed_id):
    try:
        feed = RSSFeed.query.get_or_404(feed_id)
        job_id = f'feed_{feed_id}'
        
        if scheduler.get_job(job_id):
            scheduler.remove_job(job_id)
            logger.info(f"Stopped auto-posting for feed: {feed.name}")
            return jsonify({'status': 'stopped'})
        else:
            # Validate requirements before starting
            access_token = get_config('facebook_access_token')
            if not access_token:
                return jsonify({
                    'status': 'error',
                    'message': 'Facebook access token not configured'
                }), 400
            
            post_delay = int(get_config('post_delay', '3600'))
            scheduler.add_job(
                func=post_to_facebook_auto,
                trigger=IntervalTrigger(seconds=post_delay),
                args=[feed_id],
                id=job_id,
                max_instances=1,
                misfire_grace_time=15*60  # 15 minutes grace time
            )
            logger.info(f"Started auto-posting for feed: {feed.name}")
            return jsonify({'status': 'started'})
            
    except Exception as e:
        logger.error(f"Error toggling autopost for feed {feed_id}: {str(e)}\n{traceback.format_exc()}")
        return jsonify({
            'status': 'error',
            'message': 'Internal server error'
        }), 500

@app.route('/feed/<int:feed_id>/preview')
def preview_feed(feed_id):
    try:
        feed = RSSFeed.query.get_or_404(feed_id)
        
        # Validate and parse feed
        feed_data, error = validate_feed_url(feed.url)
        if error:
            flash(error, 'error')
            return redirect(url_for('index'))
        
        # Sort entries based on post_order
        entries = feed_data.entries[:10]  # Get 10 most recent items
        if feed.post_order == 'newest':
            entries = sorted(entries, key=lambda x: x.get('published_parsed', 0), reverse=True)
        else:
            entries = sorted(entries, key=lambda x: x.get('published_parsed', 0))
        
        # Get posting conditions and status
        conditions = check_posting_conditions(feed)
        is_autoposting = bool(scheduler.get_job(f'feed_{feed_id}'))
        
        status = {
            'is_autoposting': is_autoposting,
            'is_within_window': conditions['within_window'],
            'can_post_more': conditions['within_limit'],
            'posts_today': feed.posts_today,
            'max_posts': feed.max_posts_per_day,
            'window_start': f"{feed.posting_window_start:02d}:00",
            'window_end': f"{feed.posting_window_end:02d}:59",
            'conditions': conditions
        }
        
        return render_template('preview.html',
                             feed=feed,
                             items=entries,
                             status=status,
                             scheduler=scheduler)
                             
    except Exception as e:
        logger.error(f"Error in preview route for feed {feed_id}: {str(e)}\n{traceback.format_exc()}")
        flash('Error loading feed preview', 'error')
        return redirect(url_for('index'))

@app.route('/post', methods=['POST'])
def post_to_facebook():
    try:
        # Get and validate access token
        access_token = get_config('facebook_access_token')
        if not access_token:
            flash('Facebook access token not configured', 'error')
            return redirect(url_for('index'))
        
        # Get feed and validate
        feed_id = request.form.get('feed_id')
        feed = RSSFeed.query.get_or_404(feed_id)
        
        # Check posting conditions
        conditions = check_posting_conditions(feed)
        if not conditions['within_limit']:
            flash('Daily posting limit reached', 'error')
            return redirect(url_for('preview_feed', feed_id=feed_id))
        
        if not conditions['within_window']:
            flash('Outside posting window hours', 'error')
            return redirect(url_for('preview_feed', feed_id=feed_id))
        
        if not conditions['interval_ok']:
            time_since_last = (datetime.utcnow() - feed.last_post_time).total_seconds()
            wait_time = feed.min_interval_between_posts - time_since_last
            flash(f'Please wait {int(wait_time)} seconds before posting again', 'error')
            return redirect(url_for('preview_feed', feed_id=feed_id))
        
        # Get content
        title = request.form.get('title', '')
        description = request.form.get('description', '')
        link = request.form.get('link', '')
        guid = request.form.get('guid')
        
        if not guid:
            flash('Invalid post data', 'error')
            return redirect(url_for('preview_feed', feed_id=feed_id))
        
        # Check if already posted
        if PostedItem.query.filter_by(feed_id=feed_id, item_guid=guid).first():
            flash('This item has already been posted', 'warning')
            return redirect(url_for('preview_feed', feed_id=feed_id))
        
        # Content validation
        content_length = len(title + description)
        if not (feed.min_content_length <= content_length <= feed.max_content_length):
            flash('Content length outside allowed limits', 'error')
            return redirect(url_for('preview_feed', feed_id=feed_id))
        
        # Keyword validation
        content_lower = (title + description).lower()
        if feed.required_keywords:
            required_words = [w.strip().lower() for w in feed.required_keywords.split(',')]
            if not all(word in content_lower for word in required_words):
                flash('Content missing required keywords', 'error')
                return redirect(url_for('preview_feed', feed_id=feed_id))
        
        if feed.excluded_keywords:
            excluded_words = [w.strip().lower() for w in feed.excluded_keywords.split(',')]
            if any(word in content_lower for word in excluded_words):
                flash('Content contains excluded keywords', 'error')
                return redirect(url_for('preview_feed', feed_id=feed_id))
        
        # Build message
        message = build_post_message(feed, title, description, link)
        
        # Post to Facebook
        response = post_to_facebook_api(access_token, message)
        if not response:
            flash('Error posting to Facebook', 'error')
            return redirect(url_for('preview_feed', feed_id=feed_id))
        
        # Save posted item
        save_posted_item(feed, guid, response.get('id'))
        
        flash('Posted successfully to Facebook', 'success')
        logger.info(f"Manual post successful for feed {feed.name} (ID: {feed.id})")
        
    except Exception as e:
        logger.error(f"Error in manual posting: {str(e)}\n{traceback.format_exc()}")
        flash('An error occurred while posting', 'error')
    
    return redirect(url_for('preview_feed', feed_id=feed_id))

@app.route('/feed/<int:feed_id>/settings', methods=['POST'])
def update_feed_settings(feed_id):
    try:
        feed = RSSFeed.query.get_or_404(feed_id)
        
        # Update basic settings
        feed.post_order = request.form.get('post_order', 'newest')
        feed.max_posts_per_day = int(request.form.get('max_posts_per_day') or 10)
        feed.min_interval_between_posts = int(request.form.get('min_interval_between_posts') or 300)
        
        # Update posting window
        feed.posting_window_start = int(request.form.get('posting_window_start') or 0)
        feed.posting_window_end = int(request.form.get('posting_window_end') or 23)
        
        # Update content controls
        feed.include_title = 'include_title' in request.form
        feed.include_description = 'include_description' in request.form
        feed.include_link = 'include_link' in request.form
        feed.include_media = 'include_media' in request.form
        
        # Update media controls
        feed.extract_youtube = 'extract_youtube' in request.form
        feed.extract_twitter = 'extract_twitter' in request.form
        feed.extract_tiktok = 'extract_tiktok' in request.form
        feed.extract_images = 'extract_images' in request.form
        feed.prefer_video = 'prefer_video' in request.form
        
        # Update content filtering
        feed.required_keywords = request.form.get('required_keywords', '')
        feed.excluded_keywords = request.form.get('excluded_keywords', '')
        
        # Update custom text
        feed.custom_prefix = request.form.get('custom_prefix', '')
        feed.custom_suffix = request.form.get('custom_suffix', '')
        
        db.session.commit()
        flash('Feed settings updated successfully', 'success')
    except ValueError as e:
        flash(f'Invalid value provided: {str(e)}', 'danger')
    except Exception as e:
        flash(f'Error updating feed settings: {str(e)}', 'danger')
        
    return redirect(url_for('preview_feed', feed_id=feed_id))

@app.route('/custom-posts')
def custom_posts():
    """Show custom posts page"""
    draft_posts = CustomPost.query.filter_by(status='draft').order_by(CustomPost.created_at.desc()).all()
    posted_items = CustomPost.query.filter_by(status='posted').order_by(CustomPost.posted_at.desc()).all()
    return render_template('custom_posts.html', draft_posts=draft_posts, posted_items=posted_items)

@app.route('/custom-post', methods=['POST'])
def create_custom_post():
    """Create a new custom post"""
    try:
        post = CustomPost(
            title=request.form.get('title'),
            content=request.form.get('content'),
            media_url=request.form.get('media_url'),
            link=request.form.get('link'),
            media_type='image' if request.form.get('media_url', '').lower().endswith(('.jpg', '.jpeg', '.png', '.gif')) else 'video'
        )
        db.session.add(post)
        db.session.commit()
        flash('Post created successfully', 'success')
    except Exception as e:
        flash(f'Error creating post: {str(e)}', 'danger')
    
    return redirect(url_for('custom_posts'))

@app.route('/custom-post/<int:post_id>', methods=['GET', 'PUT', 'DELETE'])
def manage_custom_post(post_id):
    """Manage a custom post"""
    post = CustomPost.query.get_or_404(post_id)
    
    if request.method == 'GET':
        return jsonify({
            'id': post.id,
            'title': post.title,
            'content': post.content,
            'media_url': post.media_url,
            'link': post.link
        })
    
    elif request.method == 'PUT':
        try:
            data = request.get_json()
            post.title = data.get('title')
            post.content = data.get('content')
            post.media_url = data.get('media_url')
            post.link = data.get('link')
            if post.media_url:
                post.media_type = 'image' if post.media_url.lower().endswith(('.jpg', '.jpeg', '.png', '.gif')) else 'video'
            db.session.commit()
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'success': False, 'message': str(e)})
    
    elif request.method == 'DELETE':
        try:
            db.session.delete(post)
            db.session.commit()
            return jsonify({'success': True})
        except Exception as e:
            return jsonify({'success': False, 'message': str(e)})

@app.route('/custom-post/<int:post_id>/post', methods=['POST'])
def post_custom_to_facebook(post_id):
    """Post a custom post to Facebook"""
    try:
        post = CustomPost.query.get_or_404(post_id)
        access_token = get_config('access_token')
        
        if not access_token:
            return jsonify({'success': False, 'message': 'Facebook access token not configured'})
        
        # Build the message
        message_parts = []
        if post.title:
            message_parts.append(post.title)
        message_parts.append(post.content)
        if post.link:
            message_parts.append(post.link)
        message = '\n\n'.join(part.strip() for part in message_parts if part and part.strip())
        
        # Post to Facebook
        if post.media_url:
            if post.media_type == 'image':
                response = requests.post(
                    'https://graph.facebook.com/v17.0/me/photos',
                    params={
                        'access_token': access_token,
                        'message': message,
                        'url': post.media_url
                    }
                )
            else:  # video
                response = requests.post(
                    'https://graph.facebook.com/v17.0/me/feed',
                    params={
                        'access_token': access_token,
                        'message': message,
                        'link': post.media_url
                    }
                )
        else:
            response = requests.post(
                'https://graph.facebook.com/v17.0/me/feed',
                params={
                    'access_token': access_token,
                    'message': message
                }
            )
        
        response.raise_for_status()
        result = response.json()
        
        # Update post status
        post.status = 'posted'
        post.posted_at = datetime.utcnow()
        post.facebook_post_id = result.get('id')
        db.session.commit()
        
        return jsonify({'success': True})
    
    except Exception as e:
        logger.error(f"Error posting to Facebook: {str(e)}\n{traceback.format_exc()}")
        return jsonify({'success': False, 'message': str(e)})

@app.route('/posted-items')
def posted_items():
    """Show all posted items with filtering and pagination"""
    page = request.args.get('page', 1, type=int)
    per_page = 20
    source = request.args.get('source', 'all')
    search = request.args.get('search', '')
    date_from = request.args.get('date_from')
    date_to = request.args.get('date_to')
    
    # Base queries
    rss_query = db.session.query(
        PostedItem.id,
        PostedItem.posted_at,
        PostedItem.facebook_post_id,
        RSSFeed.name.label('feed_name'),
        db.literal('rss').label('source_type')
    ).join(RSSFeed)
    
    custom_query = db.session.query(
        CustomPost.id,
        CustomPost.posted_at,
        CustomPost.facebook_post_id,
        db.literal(None).label('feed_name'),
        db.literal('custom').label('source_type')
    ).filter(CustomPost.status == 'posted')
    
    # Apply filters
    if search:
        rss_query = rss_query.filter(
            db.or_(
                RSSFeed.name.ilike(f'%{search}%'),
                PostedItem.item_guid.ilike(f'%{search}%')
            )
        )
        custom_query = custom_query.filter(
            db.or_(
                CustomPost.title.ilike(f'%{search}%'),
                CustomPost.content.ilike(f'%{search}%')
            )
        )
    
    if date_from:
        date_from = datetime.strptime(date_from, '%Y-%m-%d')
        rss_query = rss_query.filter(PostedItem.posted_at >= date_from)
        custom_query = custom_query.filter(CustomPost.posted_at >= date_from)
    
    if date_to:
        date_to = datetime.strptime(date_to, '%Y-%m-%d')
        rss_query = rss_query.filter(PostedItem.posted_at <= date_to)
        custom_query = custom_query.filter(CustomPost.posted_at <= date_to)
    
    # Combine queries based on source filter
    if source == 'rss':
        query = rss_query
    elif source == 'custom':
        query = custom_query
    else:
        query = rss_query.union(custom_query)
    
    # Order and paginate
    query = query.order_by(db.desc(PostedItem.posted_at))
    pagination = query.paginate(page=page, per_page=per_page)
    
    # Get stats
    stats = {
        'total_posts': PostedItem.query.count() + CustomPost.query.filter_by(status='posted').count(),
        'posts_today': (
            PostedItem.query.filter(PostedItem.posted_at >= datetime.utcnow().date()).count() +
            CustomPost.query.filter(
                CustomPost.status == 'posted',
                CustomPost.posted_at >= datetime.utcnow().date()
            ).count()
        ),
        'rss_posts': PostedItem.query.count(),
        'custom_posts': CustomPost.query.filter_by(status='posted').count()
    }
    
    return render_template('posted_items.html',
                         items=pagination.items,
                         pagination=pagination,
                         stats=stats)

@app.route('/feed/<int:feed_id>/refresh', methods=['POST'])
def refresh_feed(feed_id):
    try:
        feed = RSSFeed.query.get_or_404(feed_id)
        
        # Parse the feed
        parsed = feedparser.parse(feed.url)
        if hasattr(parsed, 'bozo_exception'):
            app.logger.error(f"Error parsing feed {feed.url}: {parsed.bozo_exception}")
            return jsonify({'success': False, 'message': 'Error parsing feed'})
        
        # Update last fetch time
        feed.last_fetch = datetime.utcnow()
        db.session.commit()
        
        app.logger.info(f"Feed {feed.url} refreshed successfully")
        return jsonify({'success': True})
        
    except Exception as e:
        app.logger.error(f"Error refreshing feed {feed_id}: {str(e)}")
        return jsonify({'success': False, 'message': 'Error refreshing feed'})

# Error handlers
@app.errorhandler(404)
def not_found_error(error):
    logger.warning(f"404 error: {request.url}")
    flash('The requested resource was not found', 'error')
    return redirect(url_for('index'))

@app.errorhandler(500)
def internal_error(error):
    logger.error(f"500 error: {str(error)}\n{traceback.format_exc()}")
    db.session.rollback()
    flash('An internal error occurred. Please try again later.', 'error')
    return redirect(url_for('index'))

if __name__ == '__main__':
    with app.app_context():
        try:
            db.create_all()
            logger.info("Database tables created successfully")
        except Exception as e:
            logger.error(f"Error creating database tables: {str(e)}")
    app.run(debug=True) 