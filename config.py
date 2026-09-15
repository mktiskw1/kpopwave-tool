import os
from dotenv import load_dotenv

load_dotenv()

# yt-dlpでダウンロードする動画の最大解像度(高さ、px)。
# Threads推奨解像度は1080x1920のため1080に設定(720pは下限であって上限ではない)。
# video_collector.py・app.pyの全ダウンロード経路(通常のYouTube収集・Music Bank検索・
# fancam検索・X収集・チャプター分割元動画・再ダウンロード)がここを参照する。
YOUTUBE_MAX_HEIGHT = 1080
YOUTUBE_DL_FORMAT = f"bestvideo[ext=mp4][height<={YOUTUBE_MAX_HEIGHT}]+bestaudio[ext=m4a]/best[ext=mp4]"


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-in-prod")
    SQLALCHEMY_DATABASE_URI = "sqlite:///rock_metal.db"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
