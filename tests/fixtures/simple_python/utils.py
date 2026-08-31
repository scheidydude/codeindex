"""Utility helpers."""

from models import User


def greet(user: User) -> str:
    return f"Hello, {user.display()}"


def slugify(text: str) -> str:
    return text.lower().replace(" ", "-")
