# ai-vault-reddit-capture

Personal script that reads my own Reddit saved posts via the Reddit API 
and stores them in a local SQLite database for private knowledge management.

## What it does
- Authenticates as my own account using a Reddit script app (OAuth2)
- Reads /user/me/saved (my saved posts only)
- Extracts article text from linked posts using trafilatura
- Inserts new posts into a local inbox table for further processing
- No posting, voting, commenting, or interaction with other users
- No subreddit scraping
- All data stays on my local machine

## Dependencies
- [PRAW](https://github.com/praw-dev/praw) — Reddit API wrapper
- [trafilatura](https://github.com/adbar/trafilatura) — article text extraction
- Flask — lightweight HTTP server
