"""Product list for the demo store. Made-up products, prices in whole paise.

A small built-in list until Joel's catalogue.json arrives. Set CATALOGUE_PATH to load a file
instead. File format: a JSON list of products shaped like the entries below
(`variations` hold `sku_id`, `spin_id`, `pack`, `mrp_paise`, `price_paise`, `max_qty`).
"""

import json
import os
from pathlib import Path
from typing import Any

HAMPER_SKU = "sku_hamper_1"
HAMPER_SPIN = "spin_hamper_1"


def _v(sku: str, pack: str, mrp: int, price: int, max_qty: int = 6, veg: bool = True) -> dict:
    return {
        "sku_id": f"sku_{sku}",
        "spin_id": f"spin_{sku}",
        "pack": pack,
        "mrp_paise": mrp * 100,
        "price_paise": price * 100,
        "max_qty": max_qty,
        "veg": veg,
        "in_stock": True,
    }


BUILTIN: list[dict[str, Any]] = [
    {
        "product_id": "p_nandini_toned",
        "name": "Nandini Toned Milk",
        "brand": "Nandini",
        "category": "milk",
        "keywords": ["dairy", "haalu"],
        "variations": [
            _v("nandini_toned_500ml", "500 ml", 28, 27, 10),
            _v("nandini_toned_1l", "1 L", 56, 55, 10),
        ],
    },
    {
        "product_id": "p_heritage_fc",
        "name": "Heritage Full Cream Milk",
        "brand": "Heritage",
        "category": "milk",
        "keywords": ["dairy", "haalu"],
        "variations": [
            _v("heritage_fc_500ml", "500 ml", 36, 34, 10),
            _v("heritage_fc_1l", "1 L", 70, 66, 10),
        ],
    },
    {
        "product_id": "p_nandini_curd",
        "name": "Nandini Good Life Curd",
        "brand": "Nandini",
        "category": "dairy",
        "keywords": ["curd", "yogurt", "mosaru"],
        "variations": [
            _v("nandini_curd_500g", "500 g", 34, 32),
            _v("nandini_curd_1kg", "1 kg", 66, 62),
        ],
    },
    {
        "product_id": "p_atta",
        "name": "Sunrise Whole Wheat Atta",
        "brand": "Sunrise",
        "category": "staples",
        "keywords": ["atta", "wheat flour", "godhi hittu"],
        "variations": [
            _v("sunrise_atta_5kg", "5 kg", 295, 285, 4),
            _v("sunrise_atta_10kg", "10 kg", 570, 549, 2),
        ],
    },
    {
        "product_id": "p_rice",
        "name": "Golden Fields Sona Masoori Rice",
        "brand": "Golden Fields",
        "category": "staples",
        "keywords": ["rice", "akki"],
        "variations": [_v("gf_rice_5kg", "5 kg", 399, 379, 4)],
    },
    {
        "product_id": "p_toor",
        "name": "Annapurna Select Toor Dal",
        "brand": "Annapurna Select",
        "category": "staples",
        "keywords": ["dal", "tuvar", "togari bele"],
        "variations": [_v("annapurna_toor_1kg", "1 kg", 165, 155, 5)],
    },
    {
        "product_id": "p_sugar",
        "name": "Sweetway Sugar",
        "brand": "Sweetway",
        "category": "staples",
        "keywords": ["sugar", "sakkare"],
        "variations": [_v("sweetway_sugar_1kg", "1 kg", 52, 48, 5)],
    },
    {
        "product_id": "p_salt",
        "name": "Saltmark Iodised Salt",
        "brand": "Saltmark",
        "category": "staples",
        "keywords": ["salt", "uppu"],
        "variations": [_v("saltmark_salt_1kg", "1 kg", 28, 25, 5)],
    },
    {
        "product_id": "p_eggs",
        "name": "Farm Fresh Eggs",
        "brand": "Farm Fresh",
        "category": "eggs",
        "keywords": ["egg", "motte"],
        "variations": [
            _v("ff_eggs_6", "6 pcs", 54, 52, 6, veg=False),
            _v("ff_eggs_12", "12 pcs", 104, 99, 4, veg=False),
        ],
    },
    {
        "product_id": "p_onion",
        "name": "Onion",
        "brand": "Fresh",
        "category": "vegetables",
        "keywords": ["onion", "eerulli"],
        "variations": [_v("fresh_onion_1kg", "1 kg", 45, 39, 5)],
    },
    {
        "product_id": "p_tomato",
        "name": "Tomato",
        "brand": "Fresh",
        "category": "vegetables",
        "keywords": ["tomato", "tamatar"],
        "variations": [_v("fresh_tomato_500g", "500 g", 28, 25, 5)],
    },
    {
        "product_id": "p_banana",
        "name": "Robusta Banana",
        "brand": "Fresh",
        "category": "fruits",
        "keywords": ["banana", "bale hannu"],
        "variations": [_v("fresh_banana_6", "6 pcs", 40, 36, 5)],
    },
    {
        "product_id": "p_oil",
        "name": "SunDrop Sunflower Oil",
        "brand": "SunDrop",
        "category": "staples",
        "keywords": ["oil", "cooking oil", "enne"],
        "variations": [
            _v("sundrop_oil_1l", "1 L", 160, 149, 4),
            _v("sundrop_oil_5l", "5 L", 780, 745, 2),
        ],
    },
    {
        "product_id": "p_tea",
        "name": "Hillside Tea Gold",
        "brand": "Hillside",
        "category": "beverages",
        "keywords": ["tea", "chai", "tea powder"],
        "variations": [
            _v("hillside_tea_250g", "250 g", 130, 122, 4),
            _v("hillside_tea_500g", "500 g", 250, 239, 3),
        ],
    },
    {
        "product_id": "p_bread",
        "name": "Daily Fresh Sandwich Bread",
        "brand": "Daily Fresh",
        "category": "bakery",
        "keywords": ["bread"],
        "variations": [_v("dailyfresh_bread_400g", "400 g", 45, 42, 5)],
    },
    {
        "product_id": "p_marie",
        "name": "Crunchy Marie Biscuits",
        "brand": "Crunchy",
        "category": "snacks",
        "keywords": ["biscuit", "marie"],
        "variations": [_v("crunchy_marie_250g", "250 g", 35, 32, 6)],
    },
    {
        "product_id": "p_hamper",
        "name": "Diwali Dry Fruit Gift Hamper",
        "brand": "Royal Harvest",
        "category": "gifting",
        "keywords": ["gift", "hamper", "dry fruits"],
        "variations": [
            {
                "sku_id": HAMPER_SKU,
                "spin_id": HAMPER_SPIN,
                "pack": "1 pc",
                "mrp_paise": 219900,
                "price_paise": 199900,
                "max_qty": 2,
                "veg": True,
                "in_stock": True,
            }
        ],
    },
]


def load_catalogue(path: str | Path | None = None) -> list[dict[str, Any]]:
    """Read a catalogue file, or fall back to the built-in list."""
    chosen = path or os.environ.get("CATALOGUE_PATH")
    if not chosen:
        return json.loads(json.dumps(BUILTIN))  # deep copy, the store edits stock flags
    return json.loads(Path(chosen).read_text(encoding="utf-8"))
