from flask_sqlalchemy import SQLAlchemy
from datetime import datetime

db = SQLAlchemy()

class RSSFeed(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    url = db.Column(db.String(500), nullable=False)
    name = db.Column(db.String(200))
    last_fetch = db.Column(db.DateTime)
    last_post_time = db.Column(db.DateTime)
    
    # Posting Controls
    post_order = db.Column(db.String(10), default='newest')
    max_posts_per_day = db.Column(db.Integer, default=10)
    posting_window_start = db.Column(db.Integer, default=0)
    posting_window_end = db.Column(db.Integer, default=23)
    posts_today = db.Column(db.Integer, default=0)
    last_post_count_reset = db.Column(db.DateTime)
    min_interval_between_posts = db.Column(db.Integer, default=300)
    
    # Content Controls
    include_title = db.Column(db.Boolean, default=True)
    include_description = db.Column(db.Boolean, default=True)
    include_link = db.Column(db.Boolean, default=True)
    include_media = db.Column(db.Boolean, default=True)
    
    # Media Controls
    extract_youtube = db.Column(db.Boolean, default=True)
    extract_twitter = db.Column(db.Boolean, default=True)
    extract_tiktok = db.Column(db.Boolean, default=True)
    extract_images = db.Column(db.Boolean, default=True)
    prefer_video = db.Column(db.Boolean, default=True)  # Prefer video over images when both are available
    
    # Content Filtering
    min_content_length = db.Column(db.Integer, default=0)
    max_content_length = db.Column(db.Integer, default=5000)
    required_keywords = db.Column(db.String(500))
    excluded_keywords = db.Column(db.String(500))
    
    # Custom Text
    custom_prefix = db.Column(db.String(500))
    custom_suffix = db.Column(db.String(500))

    # Relationships
    posted_items = db.relationship('PostedItem', backref='feed', lazy=True, cascade='all, delete-orphan')

class Config(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    key = db.Column(db.String(50), unique=True, nullable=False)
    value = db.Column(db.Text)

class PostedItem(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    feed_id = db.Column(db.Integer, db.ForeignKey('rss_feed.id'), nullable=False)
    item_guid = db.Column(db.String(500), nullable=False)
    posted_at = db.Column(db.DateTime, default=datetime.utcnow)
    facebook_post_id = db.Column(db.String(100)) 

class CustomPost(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    title = db.Column(db.String(500))
    content = db.Column(db.Text)
    media_url = db.Column(db.String(500))  # URL for image or video
    media_type = db.Column(db.String(20))  # 'image', 'video', or None
    link = db.Column(db.String(500))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)
    posted_at = db.Column(db.DateTime)
    facebook_post_id = db.Column(db.String(100))
    status = db.Column(db.String(20), default='draft')  # draft, posted, failed 