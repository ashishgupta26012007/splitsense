from datetime import datetime
from flask_sqlalchemy import SQLAlchemy
from flask_login import UserMixin

db = SQLAlchemy()


class User(UserMixin, db.Model):
    id = db.Column(db.Integer, primary_key=True)
    username = db.Column(db.String(80), unique=True, nullable=False)
    password_hash = db.Column(db.String(255), nullable=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    expenses = db.relationship("Expense", backref="user", lazy=True)


class Expense(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    group_id = db.Column(db.Integer, db.ForeignKey("group.id"), nullable=True)
    description = db.Column(db.String(255), nullable=False)
    category = db.Column(db.String(30), default="Other")
    receipt_filename = db.Column(db.String(255), nullable=True)
    amount = db.Column(db.Float, nullable=False)
    paid_by = db.Column(db.String(120), nullable=False)
    split_with = db.Column(db.String(500), nullable=False)  # comma-separated names
    date = db.Column(db.Date, default=datetime.utcnow)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

class Transaction(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    group_id = db.Column(db.Integer, db.ForeignKey("group.id"), nullable=True)
    payer = db.Column(db.String(120), nullable=False)
    receiver = db.Column(db.String(120), nullable=False)
    amount = db.Column(db.Float, nullable=False)
    is_paid = db.Column(db.Boolean, default=False)
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

import secrets


class Group(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    name = db.Column(db.String(120), nullable=False)
    description = db.Column(db.String(500))
    category = db.Column(db.String(50), default="Friends")  # Trip, Flat, Office, Friends, Family
    avatar_emoji = db.Column(db.String(10), default="👥")
    owner_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    invite_token = db.Column(db.String(32), unique=True, default=lambda: secrets.token_urlsafe(16))
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    members = db.relationship("GroupMember", backref="group", lazy=True, cascade="all, delete-orphan")


class GroupMember(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("group.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    role = db.Column(db.String(20), default="member")  # "owner", "admin", "member"
    joined_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User")


class JoinRequest(db.Model):
    id = db.Column(db.Integer, primary_key=True)
    group_id = db.Column(db.Integer, db.ForeignKey("group.id"), nullable=False)
    user_id = db.Column(db.Integer, db.ForeignKey("user.id"), nullable=False)
    status = db.Column(db.String(20), default="pending")  # "pending", "approved", "rejected"
    created_at = db.Column(db.DateTime, default=datetime.utcnow)

    user = db.relationship("User")