import os
from dotenv import load_dotenv

load_dotenv()


class Config:
    SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-in-prod")
    SQLALCHEMY_DATABASE_URI = "sqlite:///rock_metal.db"
    SQLALCHEMY_TRACK_MODIFICATIONS = False
