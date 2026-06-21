"""
NUMBER INFO BOT - Premium Professional Telegram Bot
Author: AI Assistant
Version: 3.0.0
Python: 3.11+
Hosting: GSM Telegram Bot Hosting, Render.com, Railway, VPS
"""

import asyncio
import logging
import os
import re
import sqlite3
import sys
import time
from contextlib import contextmanager
from datetime import datetime, timedelta
from typing import Dict, List, Optional, Tuple, Union
from functools import wraps
import random

import phonenumbers
from phonenumbers import carrier, geocoder, timezone, PhoneNumberType
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    Update,
    User,
    Chat,
    ChatMember,
    InputMediaPhoto,
    constants,
)
from telegram.constants import ParseMode
from telegram.ext import (
    Application,
    ApplicationBuilder,
    CommandHandler,
    CallbackQueryHandler,
    MessageHandler,
    filters,
    ContextTypes,
    CallbackContext,
)
from telegram.error import (
    TelegramError,
    NetworkError,
    TimedOut,
    BadRequest,
    Forbidden,
    Conflict,
)

# ============================================================================
# CONFIGURATION
# ============================================================================

BOT_TOKEN = os.environ.get("8865745157:AAGYzXe85C_DEPjozeTzkWvfnKZlaRbbNjQ")
if not BOT_TOKEN:
    raise ValueError("❌ BOT_TOKEN environment variable is required")

ADMIN_IDS = [int(id.strip()) for id in os.environ.get("7615625978", "").split(",") if id.strip()]
DATABASE_PATH = os.environ.get("DATABASE_PATH", "number_info_bot.db")
LOG_LEVEL = os.environ.get("LOG_LEVEL", "INFO").upper()
COOLDOWN_SECONDS = int(os.environ.get("COOLDOWN_SECONDS", 5))
RATE_LIMIT_WINDOW = int(os.environ.get("RATE_LIMIT_WINDOW", 60))
RATE_LIMIT_MAX_CALLS = int(os.environ.get("RATE_LIMIT_MAX_CALLS", 10))
DEFAULT_COUNTRY = os.environ.get("DEFAULT_COUNTRY", "US")
BOT_VERSION = "3.0.0"
BOT_NAME = "Number Info Bot"

# ============================================================================
# LOGGING
# ============================================================================

logging.basicConfig(
    level=getattr(logging, LOG_LEVEL),
    format="%(asctime)s - %(name)s - %(levelname)s - %(message)s",
    handlers=[
        logging.StreamHandler(sys.stdout),
        logging.FileHandler("bot.log", encoding="utf-8"),
    ],
)
logger = logging.getLogger(__name__)

# ============================================================================
# DATABASE LAYER
# ============================================================================

class Database:
    """SQLite database handler with connection pooling and context manager support."""
    
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._init_db()
    
    @contextmanager
    def get_connection(self):
        """Context manager for database connections with automatic commit/rollback."""
        conn = sqlite3.connect(self.db_path, check_same_thread=False)
        conn.row_factory = sqlite3.Row
        try:
            yield conn
            conn.commit()
        except Exception as e:
            conn.rollback()
            logger.error(f"Database transaction failed: {e}")
            raise
        finally:
            conn.close()
    
    def _init_db(self) -> None:
        """Initialize database schema with proper indexes and constraints."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Users table - stores user metadata and statistics
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS users (
                    user_id INTEGER PRIMARY KEY,
                    username TEXT,
                    first_name TEXT,
                    last_name TEXT,
                    language_code TEXT,
                    is_bot BOOLEAN DEFAULT 0,
                    joined_date TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    last_active TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    total_requests INTEGER DEFAULT 0,
                    total_searches INTEGER DEFAULT 0,
                    is_admin BOOLEAN DEFAULT 0,
                    is_blocked BOOLEAN DEFAULT 0,
                    UNIQUE(user_id)
                )
            """)
            
            # Phone number lookup history
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS phone_lookups (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    phone_number TEXT,
                    country TEXT,
                    country_code TEXT,
                    carrier TEXT,
                    timezone TEXT,
                    number_type TEXT,
                    is_valid BOOLEAN,
                    lookup_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY (user_id) REFERENCES users (user_id) ON DELETE CASCADE
                )
            """)
            
            # Cooldown tracking
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS cooldown_tracker (
                    user_id INTEGER PRIMARY KEY,
                    last_command_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                    command_count INTEGER DEFAULT 0
                )
            """)
            
            # Rate limiting log
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS rate_log (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    user_id INTEGER,
                    action TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Admin audit log
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS admin_audit (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_id INTEGER,
                    action TEXT,
                    target_user_id INTEGER,
                    details TEXT,
                    timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Broadcast history
            cursor.execute("""
                CREATE TABLE IF NOT EXISTS broadcasts (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    admin_id INTEGER,
                    message TEXT,
                    recipients INTEGER,
                    sent_timestamp TIMESTAMP DEFAULT CURRENT_TIMESTAMP
                )
            """)
            
            # Create indexes for performance
            cursor.executescript("""
                CREATE INDEX IF NOT EXISTS idx_phone_lookups_user_id ON phone_lookups(user_id);
                CREATE INDEX IF NOT EXISTS idx_phone_lookups_timestamp ON phone_lookups(lookup_timestamp);
                CREATE INDEX IF NOT EXISTS idx_rate_log_user_id ON rate_log(user_id);
                CREATE INDEX IF NOT EXISTS idx_rate_log_timestamp ON rate_log(timestamp);
                CREATE INDEX IF NOT EXISTS idx_users_last_active ON users(last_active);
                CREATE INDEX IF NOT EXISTS idx_users_is_admin ON users(is_admin);
                CREATE INDEX IF NOT EXISTS idx_cooldown_tracker_user_id ON cooldown_tracker(user_id);
            """)
            
            logger.info("✅ Database initialized successfully")
    
    # -------------------------------------------------------------------------
    # USER MANAGEMENT
    # -------------------------------------------------------------------------
    
    def register_user(self, user: User) -> None:
        """Register or update user in the database."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO users (user_id, username, first_name, last_name, language_code, is_bot)
                VALUES (?, ?, ?, ?, ?, ?)
                ON CONFLICT(user_id) DO UPDATE SET
                    username = COALESCE(excluded.username, username),
                    first_name = COALESCE(excluded.first_name, first_name),
                    last_name = COALESCE(excluded.last_name, last_name),
                    language_code = COALESCE(excluded.language_code, language_code),
                    last_active = CURRENT_TIMESTAMP
            """, (
                user.id,
                user.username,
                user.first_name,
                user.last_name,
                user.language_code,
                user.is_bot,
            ))
    
    def update_user_activity(self, user_id: int) -> None:
        """Update the last_active timestamp for a user."""
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE users SET last_active = CURRENT_TIMESTAMP WHERE user_id = ?",
                (user_id,)
            )
    
    def increment_user_requests(self, user_id: int) -> None:
        """Increment the total request count for a user."""
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE users SET total_requests = total_requests + 1 WHERE user_id = ?",
                (user_id,)
            )
    
    def increment_user_searches(self, user_id: int) -> None:
        """Increment the total search count for a user."""
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE users SET total_searches = total_searches + 1 WHERE user_id = ?",
                (user_id,)
            )
    
    def get_user_stats(self, user_id: int) -> Optional[Dict]:
        """Retrieve all statistics for a specific user."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT 
                    u.*,
                    COUNT(pl.id) as total_lookups,
                    MAX(pl.lookup_timestamp) as last_lookup
                FROM users u
                LEFT JOIN phone_lookups pl ON u.user_id = pl.user_id
                WHERE u.user_id = ?
                GROUP BY u.user_id
            """, (user_id,))
            row = cursor.fetchone()
            return dict(row) if row else None
    
    def is_admin(self, user_id: int) -> bool:
        """Check if a user has admin privileges."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT is_admin FROM users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            return row is not None and row['is_admin'] == 1
    
    def set_admin_status(self, user_id: int, is_admin: bool) -> None:
        """Grant or revoke admin privileges for a user."""
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE users SET is_admin = ? WHERE user_id = ?",
                (1 if is_admin else 0, user_id)
            )
    
    def block_user(self, user_id: int) -> None:
        """Block a user from using the bot."""
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE users SET is_blocked = 1 WHERE user_id = ?",
                (user_id,)
            )
    
    def unblock_user(self, user_id: int) -> None:
        """Unblock a user."""
        with self.get_connection() as conn:
            conn.execute(
                "UPDATE users SET is_blocked = 0 WHERE user_id = ?",
                (user_id,)
            )
    
    def is_user_blocked(self, user_id: int) -> bool:
        """Check if a user is blocked."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT is_blocked FROM users WHERE user_id = ?", (user_id,))
            row = cursor.fetchone()
            return row is not None and row['is_blocked'] == 1
    
    def get_all_users(self, limit: int = 1000, offset: int = 0) -> List[Dict]:
        """Retrieve paginated list of all users."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT user_id, username, first_name, last_name, joined_date, 
                       last_active, total_requests, total_searches, is_admin, is_blocked
                FROM users
                ORDER BY joined_date DESC
                LIMIT ? OFFSET ?
            """, (limit, offset))
            return [dict(row) for row in cursor.fetchall()]
    
    def get_total_users(self) -> int:
        """Get total number of registered users."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM users")
            return cursor.fetchone()[0]
    
    def get_active_users_last_24h(self) -> int:
        """Count users active in the last 24 hours."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(DISTINCT user_id) 
                FROM phone_lookups 
                WHERE lookup_timestamp > datetime('now', '-1 day')
            """)
            return cursor.fetchone()[0]
    
    def get_total_searches(self) -> int:
        """Get total number of phone number searches."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("SELECT COUNT(*) FROM phone_lookups")
            return cursor.fetchone()[0]
    
    # -------------------------------------------------------------------------
    # PHONE LOOKUP TRACKING
    # -------------------------------------------------------------------------
    
    def log_phone_lookup(
        self,
        user_id: int,
        phone_number: str,
        country: str,
        country_code: str,
        carrier_name: str,
        timezone_list: List[str],
        number_type: str,
        is_valid: bool,
    ) -> None:
        """Record a phone number lookup in the database."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                INSERT INTO phone_lookups (
                    user_id, phone_number, country, country_code, 
                    carrier, timezone, number_type, is_valid
                ) VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """, (
                user_id,
                phone_number,
                country,
                country_code,
                carrier_name,
                ", ".join(timezone_list),
                number_type,
                is_valid,
            ))
            self.increment_user_requests(user_id)
            self.increment_user_searches(user_id)
    
    def get_phone_lookup_history(
        self,
        user_id: int,
        limit: int = 10
    ) -> List[Dict]:
        """Retrieve recent phone lookups for a user."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT phone_number, country, country_code, carrier, 
                       timezone, number_type, is_valid, lookup_timestamp
                FROM phone_lookups
                WHERE user_id = ?
                ORDER BY lookup_timestamp DESC
                LIMIT ?
            """, (user_id, limit))
            return [dict(row) for row in cursor.fetchall()]
    
    def get_global_stats(self) -> Dict:
        """Get global bot statistics."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            
            # Total lookups
            cursor.execute("SELECT COUNT(*) FROM phone_lookups")
            total_lookups = cursor.fetchone()[0]
            
            # Valid vs invalid
            cursor.execute(
                "SELECT is_valid, COUNT(*) FROM phone_lookups GROUP BY is_valid"
            )
            validity_counts = {
                bool(row['is_valid']): row[1] for row in cursor.fetchall()
            }
            
            # Most looked up countries
            cursor.execute("""
                SELECT country, COUNT(*) as cnt
                FROM phone_lookups
                WHERE country IS NOT NULL
                GROUP BY country
                ORDER BY cnt DESC
                LIMIT 5
            """)
            top_countries = [dict(row) for row in cursor.fetchall()]
            
            # Lookups per day (last 7 days)
            cursor.execute("""
                SELECT DATE(lookup_timestamp) as date, COUNT(*) as cnt
                FROM phone_lookups
                WHERE lookup_timestamp > datetime('now', '-7 days')
                GROUP BY DATE(lookup_timestamp)
                ORDER BY date DESC
            """)
            daily_lookups = [dict(row) for row in cursor.fetchall()]
            
            return {
                "total_lookups": total_lookups,
                "valid_lookups": validity_counts.get(True, 0),
                "invalid_lookups": validity_counts.get(False, 0),
                "top_countries": top_countries,
                "daily_lookups": daily_lookups,
                "total_users": self.get_total_users(),
                "active_users_24h": self.get_active_users_last_24h(),
                "total_searches": total_lookups,
            }
    
    # -------------------------------------------------------------------------
    # COOLDOWN & RATE LIMITING
    # -------------------------------------------------------------------------
    
    def check_cooldown(self, user_id: int, cooldown_seconds: int) -> Tuple[bool, int]:
        """
        Check if user is in cooldown.
        Returns: (is_allowed, remaining_seconds)
        """
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT last_command_timestamp, command_count
                FROM cooldown_tracker
                WHERE user_id = ?
            """, (user_id,))
            row = cursor.fetchone()
            
            if row is None:
                # First command, create record
                conn.execute(
                    "INSERT INTO cooldown_tracker (user_id, last_command_timestamp, command_count) VALUES (?, CURRENT_TIMESTAMP, 1)",
                    (user_id,)
                )
                return True, 0
            
            last_time = datetime.fromisoformat(row['last_command_timestamp'])
            elapsed = (datetime.now() - last_time).total_seconds()
            
            if elapsed >= cooldown_seconds:
                # Reset cooldown
                conn.execute(
                    "UPDATE cooldown_tracker SET last_command_timestamp = CURRENT_TIMESTAMP, command_count = 1 WHERE user_id = ?",
                    (user_id,)
                )
                return True, 0
            else:
                remaining = int(cooldown_seconds - elapsed)
                # Increment command count
                conn.execute(
                    "UPDATE cooldown_tracker SET command_count = command_count + 1 WHERE user_id = ?",
                    (user_id,)
                )
                return False, remaining
    
    def log_rate_action(self, user_id: int, action: str) -> None:
        """Log a user action for rate limiting purposes."""
        with self.get_connection() as conn:
            conn.execute("""
                INSERT INTO rate_log (user_id, action)
                VALUES (?, ?)
            """, (user_id, action))
    
    def get_rate_count(
        self,
        user_id: int,
        action: str,
        window_seconds: int,
    ) -> int:
        """Count actions for a user within a time window."""
        with self.get_connection() as conn:
            cursor = conn.cursor()
            cursor.execute("""
                SELECT COUNT(*) 
                FROM rate_log 
                WHERE user_id = ? 
                    AND action = ? 
                    AND timestamp > datetime('now', ?)
            """, (user_id, action, f"-{window_seconds} seconds"))
            return cursor.fetchone()[0]
    
    # ------------------------------------------------------