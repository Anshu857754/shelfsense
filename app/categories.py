"""Retail categories.

A category tells the vision model what kind of shelf it is looking at and how to count
(for example garments instead of bottles). It does not limit what can be detected:
the planogram the user writes decides which products are expected, and the model reports
whatever it actually sees. The samples below only pre-fill the planogram editor.
"""

from dataclasses import dataclass

DEFAULT_CATEGORY = "grocery"
CUSTOM_CATEGORY = "custom"


@dataclass(frozen=True)
class Category:
    key: str
    label: str
    unit: str  # what one "facing" means for this category
    hint: str  # extra guidance given to the vision model
    sample: dict  # example planogram for the editor's "Load sample" button


def _plan(store: str, shelf: str, rows: list[list[tuple]]) -> dict:
    """rows: [[(product, facings, [aliases]), ...], ...] -> planogram dict."""
    return {
        "store": store,
        "shelf": shelf,
        "rows": [
            {
                "row": i,
                "expected": [
                    {"product": p, "facings": f, **({"aliases": a} if a else {})}
                    for p, f, a in items
                ],
            }
            for i, items in enumerate(rows, start=1)
        ],
    }


CATEGORIES: dict[str, Category] = {
    c.key: c
    for c in [
        Category(
            "grocery",
            "Grocery and snacks",
            "packs standing side by side at the front",
            "Look for packaged food: chips, biscuits, cereal, noodles, sauces, tins. "
            "Name products by type and brand if the label is readable.",
            _plan(
                "Store-101",
                "Snacks-A",
                [
                    [("Chips bag", 6, ["snack bag", "tortilla chips bag"]), ("Cereal box", 4, [])],
                    [("Biscuit pack", 5, ["cookie pack"]), ("Instant noodles", 4, ["noodle pack"])],
                ],
            ),
        ),
        Category(
            "beverages",
            "Beverages",
            "bottles, cans or cartons standing side by side at the front",
            "Look for bottles, cans, cartons and multipacks. Distinguish sizes and flavours "
            "when the label is readable.",
            _plan(
                "Store-101",
                "Drinks-A",
                [
                    [("Cola bottle 500ml", 6, ["cola bottle", "coke"]), ("Orange juice carton", 4, ["juice carton"])],
                    [("Mineral water 1L", 6, ["water bottle"]), ("Energy drink can", 4, ["energy can"])],
                ],
            ),
        ),
        Category(
            "clothing",
            "Clothing and fashion",
            "garments visible at the front (hanging items, or folded stacks counted by visible stacks)",
            "Identify garment types such as shirts, T-shirts, jeans, trousers, dresses, jackets, "
            "skirts, hoodies and shoes. Name each by type and main colour (for example "
            "'blue denim jeans'). Count hanging garments one by one; for folded piles count "
            "each visible stack as one facing and mention the pile height in attributes.",
            _plan(
                "Store-101",
                "Apparel-A",
                [
                    [("Shirt", 6, ["t-shirt", "button-down shirt"]), ("Jeans", 5, ["denim jeans", "denim"])],
                    [("Trousers", 4, ["chinos", "pants"]), ("Dress", 3, []), ("Jacket", 3, ["coat", "blazer"])],
                ],
            ),
        ),
        Category(
            "cosmetics",
            "Cosmetics and skincare",
            "products standing side by side at the front",
            "Look for skincare, makeup and haircare: creams, serums, lipsticks, foundations, "
            "tubes, jars, bottles and boxed sets. Read brand and product text when legible.",
            _plan(
                "Store-101",
                "Beauty-A",
                [
                    [("Face moisturizer", 4, ["moisturiser", "face cream"]), ("Lipstick", 6, ["lip color"])],
                    [("Sunscreen tube", 4, ["sunscreen", "sun cream"]), ("Shampoo bottle", 3, ["shampoo"])],
                ],
            ),
        ),
        Category(
            "electronics",
            "Electronics",
            "boxes or devices standing side by side at the front",
            "Look for boxed devices and accessories: phones, chargers, cables, earphones, "
            "power banks, speakers. Read model names when legible.",
            _plan(
                "Store-101",
                "Gadgets-A",
                [
                    [("Phone charger", 5, ["charger", "wall adapter"]), ("Wired earphones", 4, ["earphones", "earbuds"])],
                    [("Power bank", 3, []), ("USB cable", 6, ["charging cable"]), ("Bluetooth speaker", 2, ["speaker"])],
                ],
            ),
        ),
        Category(
            "pharmacy",
            "Pharmacy and personal care",
            "boxes or bottles standing side by side at the front",
            "Look for medicines, vitamins, first-aid items, hygiene and personal care products. "
            "Do not guess drug names you cannot read.",
            _plan(
                "Store-101",
                "Pharmacy-A",
                [
                    [("Pain relief tablets", 5, ["painkiller box"]), ("Hand sanitizer", 4, ["sanitiser bottle"])],
                    [("Bandage pack", 6, ["plasters"]), ("Vitamin bottle", 4, ["supplement bottle"]), ("Toothpaste", 5, ["toothpaste tube"])],
                ],
            ),
        ),
        Category(
            "household",
            "Household products",
            "bottles, packs or rolls standing side by side at the front",
            "Look for cleaning products, detergents, paper goods, bin bags and kitchen items.",
            _plan(
                "Store-101",
                "Home-A",
                [
                    [("Detergent bottle", 4, ["laundry detergent"]), ("Dish soap", 5, ["dishwashing liquid"])],
                    [("Paper towel roll", 6, ["kitchen roll"]), ("Trash bags", 4, ["bin bags"]), ("Sponge pack", 5, ["scourer"])],
                ],
            ),
        ),
        Category(
            CUSTOM_CATEGORY,
            "Custom category",
            "items standing or hanging side by side at the front",
            "This is a custom category. Identify each product from what you can see, using the "
            "planogram names where they apply.",
            _plan("Store-101", "Shelf-A", [[("Product A", 4, []), ("Product B", 3, [])]]),
        ),
    ]
}


def get_category(key: str | None) -> Category:
    """Look up a category; unknown keys fall back to the custom category."""
    return CATEGORIES.get(key or "", CATEGORIES[CUSTOM_CATEGORY])


def category_label(key: str | None, custom_name: str = "") -> str:
    """Human-readable name, using the user's own wording for a custom category."""
    if key == CUSTOM_CATEGORY and custom_name.strip():
        return custom_name.strip()
    return get_category(key).label
