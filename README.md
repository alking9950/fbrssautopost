# RSS-to-Facebook Auto Poster

A lightweight Flask application that allows you to automatically post content from RSS feeds to Facebook pages or groups.

## Features

- RSS feed integration using feedparser
- Facebook Graph API integration for posting
- Simple web interface for managing:
  - RSS feeds
  - Facebook access token
  - Post customization
- Manual posting control
- SQLite database for configuration storage

## Setup

1. Clone this repository
2. Install dependencies:
   ```bash
   pip install -r requirements.txt
   ```
3. Create a `.env` file with your configuration:
   ```
   FLASK_SECRET_KEY=your-secret-key
   ```
4. Run the application:
   ```bash
   python app.py
   ```

## Usage

1. Access the web interface at `http://localhost:5000`
2. Add your Facebook access token
3. Configure your RSS feed(s)
4. Start posting content to Facebook

## Requirements

- Python 3.7+
- Facebook Page/Group access token with posting permissions
- Valid RSS feed URL(s) 