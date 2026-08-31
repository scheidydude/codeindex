"""Entry point."""

from models import Product, User
from utils import greet, slugify


def run() -> None:
    user = User("Alice", "alice@example.com")
    product = Product("SKU-001", 9.99)
    print(greet(user))
    print(slugify(product.sku))


if __name__ == "__main__":
    run()
