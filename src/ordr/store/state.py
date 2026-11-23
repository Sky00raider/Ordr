"""In-memory demo store. One account (the kid's), one server-side cart.

Behaviour copied from Swiggy Instamart's published MCP tools: `update_cart` replaces the whole
cart, `checkout` takes no items and buys whatever is in the cart, UPI orders start in
PENDING_PAYMENT and `confirm_order` completes them, there is no cancel tool, Rs 99 minimum.
Our assumptions, not from the docs: unpaid UPI orders fail after 5 minutes, delivery and
handling fees, the duplicate window, and that checkout delivers to the cart's address.
Money is whole paise inside. Rupee values appear only in responses, as the docs show them.
"""

import itertools
import threading
import time
from collections.abc import Callable
from decimal import Decimal
from typing import Any

from ordr.store.catalogue import HAMPER_SKU, HAMPER_SPIN, load_catalogue

MIN_ORDER_PAISE = 9900
HANDLING_FEE_PAISE = 500
DELIVERY_FEE_PAISE = 2500
FREE_DELIVERY_FROM_PAISE = 19900
UNPAID_EXPIRY_S = 300
DUPLICATE_WINDOW_S = 600
PAGE_SIZE = 10

ADDRESSES = [
    {
        "id": "addr_home",
        "addressLine": "12, 3rd Cross, Jayanagar 4th Block, Bengaluru 560011",
        "phoneNumber": "9000000001",
        "addressCategory": "Home",
        "addressTag": "Home",
    },
    {
        "id": "addr_other",
        "addressLine": "Flat 7, Lake View Apartments, HSR Layout, Bengaluru 560102",
        "phoneNumber": "9000000002",
        "addressCategory": "Other",
        "addressTag": "Friend",
    },
]


class StoreError(Exception):
    """Becomes a tool error with this message."""


def rupees(paise: int) -> float:
    return float(Decimal(paise) / 100)


def money(paise: int) -> str:
    return f"{Decimal(paise) / 100:.2f}"


class DemoStore:
    def __init__(self, clock: Callable[[], float] = time.time) -> None:
        self.clock = clock
        self._lock = threading.RLock()
        self.reset()

    # ----- setup and demo switches -------------------------------------------------------

    def reset(self) -> None:
        with self._lock:
            self.products = load_catalogue()
            self.cart: dict[str, int] = {}  # spin_id -> qty
            self.cart_address_id = "addr_home"
            self.extra_fee_paise = 0
            self.orders: list[dict[str, Any]] = []
            self._ids = itertools.count(100001)

    def switch_add_expensive_item(self) -> None:
        """As if another AI assistant on the same account added a Rs 1,999 item."""
        with self._lock:
            self.cart[HAMPER_SPIN] = self.cart.get(HAMPER_SPIN, 0) + 1

    def switch_change_address(self, address_id: str = "addr_other") -> None:
        with self._lock:
            self._address(address_id)
            self.cart_address_id = address_id

    def switch_extra_fee(self, rupees_: int) -> None:
        """Surge fee on top of the bill. Demo uses 18 and 250. 0 clears it."""
        with self._lock:
            self.extra_fee_paise = int(rupees_) * 100

    def switch_out_of_stock(self, sku_id: str, out: bool = True) -> None:
        with self._lock:
            variation = self._variation_by_sku(sku_id)
            variation["in_stock"] = not out

    def switch_preload_cart(self) -> None:
        """Start with items already in the kid's cart."""
        with self._lock:
            self.cart = {"spin_crunchy_marie_250g": 2, "spin_ff_eggs_12": 1}

    # ----- lookups -----------------------------------------------------------------------

    def _address(self, address_id: str | None) -> dict:
        for address in ADDRESSES:
            if address["id"] == address_id:
                return address
        raise StoreError(f"Unknown addressId {address_id!r}. Use an id from get_addresses.")

    def _find(self, sku_id: str | None, spin_id: str | None) -> tuple[dict, dict] | None:
        for product in self.products:
            for variation in product["variations"]:
                if variation["spin_id"] == spin_id and (
                    sku_id is None or variation["sku_id"] == sku_id
                ):
                    return product, variation
        return None

    def _variation_by_sku(self, sku_id: str) -> dict:
        for product in self.products:
            for variation in product["variations"]:
                if variation["sku_id"] == sku_id:
                    return variation
        raise StoreError(f"Unknown skuId {sku_id!r}")

    def _expire(self) -> None:
        now = self.clock()
        for order in self.orders:
            if (
                order["status"] == "PENDING_PAYMENT"
                and now - order["created_at"] >= UNPAID_EXPIRY_S
            ):
                order["status"] = "FAILED"
                order["payment_status"] = "FAILED"
                order["updated_at"] = now

    # ----- addresses and search ----------------------------------------------------------

    def get_addresses(self) -> dict:
        with self._lock:
            return {
                "addresses": [dict(a) for a in ADDRESSES],
                "pagination": {
                    "page": 1,
                    "pageSize": PAGE_SIZE,
                    "total": len(ADDRESSES),
                    "totalPages": 1,
                    "hasMore": False,
                },
                "resolution": {
                    "defaultAddressId": ADDRESSES[0]["id"],
                    "resolutionSource": "ranked_saved_address",
                    "needsUserClarification": len(ADDRESSES) > 1,
                    "candidateAddressIds": [a["id"] for a in ADDRESSES],
                },
            }

    @staticmethod
    def _search_product(product: dict) -> dict:
        variations = [
            {
                "spinId": v["spin_id"],
                "skuId": v["sku_id"],
                "quantityDescription": v["pack"],
                "displayName": f"{product['name']} {v['pack']}",
                "brandName": product["brand"],
                "price": {"mrp": rupees(v["mrp_paise"]), "offerPrice": rupees(v["price_paise"])},
                "isInStockAndAvailable": v["in_stock"],
                "vegClassifier": "VEG" if v.get("veg", True) else "NON_VEG",
                "maxQuantity": v["max_qty"],
            }
            for v in product["variations"]
        ]
        any_stock = any(v["isInStockAndAvailable"] for v in variations)
        return {
            "displayName": product["name"],
            "brand": product["brand"],
            "inStock": any_stock,
            "isAvail": any_stock,
            "productId": product["product_id"],
            "parentProductId": product["product_id"],
            "variations": variations,
        }

    def search_products(self, address_id: str | None, query: str, offset: int = 0) -> dict:
        with self._lock:
            if not address_id:
                raise StoreError("addressId is required. Use an id from get_addresses.")
            self._address(address_id)
            tokens = query.lower().split()
            if not tokens:
                raise StoreError("query cannot be empty")

            def haystack(p: dict) -> str:
                return " ".join([p["name"], p["brand"], p["category"], *p["keywords"]]).lower()

            hits = [p for p in self.products if all(t in haystack(p) for t in tokens)]
            page = hits[offset : offset + PAGE_SIZE]
            categories = {p["category"] for p in hits}
            similar = [p for p in self.products if p["category"] in categories and p not in hits]
            data: dict[str, Any] = {
                "nextOffset": str(offset + len(page)),
                "products": [self._search_product(p) for p in page],
            }
            if similar:
                data["similarProducts"] = [self._search_product(p) for p in similar[:3]]
            return data

    # ----- cart --------------------------------------------------------------------------

    def _cart_item(self, spin_id: str, qty: int) -> dict:
        found = self._find(None, spin_id)
        assert found, spin_id
        product, v = found
        return {
            "spinId": v["spin_id"],
            "skuId": v["sku_id"],
            "productId": product["product_id"],
            "itemName": product["name"],
            "itemVariant": v["pack"],
            "quantity": qty,
            "isInStockAndAvailable": v["in_stock"],
            "mrp": rupees(v["mrp_paise"]),
            "discountedFinalPrice": rupees(v["price_paise"]),
            "maxQuantity": v["max_qty"],
        }

    def _bill(self) -> tuple[int, dict[str, int], int]:
        """Item total of in-stock lines, fee lines, to-pay. All in paise."""
        item_total = 0
        for spin_id, qty in self.cart.items():
            _, v = self._find(None, spin_id)  # type: ignore[misc]
            if v["in_stock"]:
                item_total += v["price_paise"] * qty
        fees: dict[str, int] = {}
        if item_total:
            fees["Delivery fee"] = (
                0 if item_total >= FREE_DELIVERY_FROM_PAISE else DELIVERY_FEE_PAISE
            )
            fees["Handling fee"] = HANDLING_FEE_PAISE
            if self.extra_fee_paise:
                fees["Surge fee"] = self.extra_fee_paise
        return item_total, fees, item_total + sum(fees.values())

    def _cart_view(self) -> dict:
        item_total, fees, to_pay = self._bill()
        items = [self._cart_item(s, q) for s, q in self.cart.items()]
        address = self._address(self.cart_address_id)
        view: dict[str, Any] = {
            "selectedAddress": address["id"],
            "selectedAddressDetails": {
                "id": address["id"],
                "address": address["addressLine"],
                "area": address["addressLine"].split(",")[-2].strip(),
                "name": "Demo account",
                "mobile": address["phoneNumber"],
                "category": address["addressCategory"],
                "city": "Bengaluru",
            },
            "cartTotalAmount": money(to_pay),
            "items": items,
            "billBreakdown": {
                "lineItems": [
                    {"label": "Item total", "value": money(item_total)},
                    *({"label": k, "value": money(v)} for k, v in fees.items()),
                ],
                "toPay": {"label": "To pay", "value": money(to_pay)},
            },
        }
        if not items:
            view["cartAbsent"] = True
            view["cartAbsentReason"] = "Cart is empty"
        else:
            view["cartId"] = "cart_demo_1"
            unserviceable = [i for i in items if not i["isInStockAndAvailable"]]
            if unserviceable:
                view["unserviceableItems"] = unserviceable
            if 0 < item_total < MIN_ORDER_PAISE:
                view["cartWarning"] = {
                    "statusCode": 1001,
                    "message": f"Minimum order value is Rs {MIN_ORDER_PAISE // 100}",
                }
        return view

    def get_cart(self) -> dict:
        with self._lock:
            view = self._cart_view()
            view["availablePaymentMethods"] = ["UPI"]
            return view

    def update_cart(self, address_id: str | None, items: list[dict]) -> dict:
        """Replaces the whole cart. Whatever was there is gone."""
        with self._lock:
            if address_id is not None:
                self._address(address_id)
            new_cart: dict[str, int] = {}
            removed: list[dict] = []
            reduced: list[dict] = []
            for raw in items:
                spin_id, sku_id, qty = raw.get("spinId"), raw.get("skuId"), raw.get("quantity")
                if not spin_id or not sku_id or not isinstance(qty, int) or isinstance(qty, bool):
                    raise StoreError("Each item needs spinId, skuId and an integer quantity")
                found = self._find(sku_id, spin_id)
                if not found:
                    raise StoreError(
                        f"Unknown item {sku_id}/{spin_id}. Pass ids from search_products."
                    )
                if qty <= 0:
                    continue
                if not found[1]["in_stock"]:
                    removed.append(self._cart_item(spin_id, qty))
                    continue
                total_qty = new_cart.get(spin_id, 0) + qty
                cap = found[1]["max_qty"]
                if total_qty > cap:
                    reduced.append(
                        {
                            "spinId": spin_id,
                            "itemName": found[0]["name"],
                            "requestedQuantity": total_qty,
                            "cappedQuantity": cap,
                            "reason": f"Maximum {cap} per order",
                        }
                    )
                    total_qty = cap
                new_cart[spin_id] = total_qty
            self.cart = new_cart
            if address_id is not None:
                self.cart_address_id = address_id
            view = self._cart_view()
            if removed:
                view["removedOutOfStockItems"] = removed
            if reduced:
                view["reducedQuantityItems"] = reduced
            return view

    def clear_cart(self) -> dict:
        with self._lock:
            self.cart = {}
            return {"verified": True}

    # ----- payment and orders ------------------------------------------------------------

    def get_payment_options(self) -> dict:
        with self._lock:
            upi = [
                {
                    "id": "demo_upi://pay/",
                    "displayName": "Demo UPI app",
                    "kind": "intent",
                    "groupName": "UPI",
                    "enabled": True,
                }
            ]
            _, _, to_pay = self._bill()
            return {
                "platforms": {"mobile": {"groupName": "UPI", "methods": upi}},
                "agenticPaymentEligible": True,
                "allMethods": upi,
                "paymentAmount": money(to_pay) if to_pay else None,
                "placeOrderToolName": "checkout",
            }

    def _lines_key(self, order: dict) -> dict[str, int]:
        return {line["skuId"]: line["quantity"] for line in order["lines"]}

    def checkout(
        self,
        address_id: str,
        payment_method: str | None = None,
        intent_app: str | None = None,
        generate_upi_qr: bool | None = None,
        confirm_duplicate: bool | None = None,
    ) -> dict:
        """Buys whatever is in the server-side cart. Takes no items."""
        with self._lock:
            self._expire()
            self._address(address_id)
            method = (payment_method or "UPI").strip()
            if method.upper() == "UPI":
                method = "UPI"
            elif method.upper() in ("CASH", "COD"):
                method = "Cash"
            else:
                raise StoreError(f"Unsupported paymentMethod {payment_method!r}")
            if not self.cart:
                raise StoreError("Cart is empty")
            lines = [self._cart_item(s, q) for s, q in self.cart.items()]
            if any(not line["isInStockAndAvailable"] for line in lines):
                raise StoreError("Some items are out of stock. Refresh the cart.")
            item_total, fees, to_pay = self._bill()
            if item_total < MIN_ORDER_PAISE:
                raise StoreError(f"Minimum order value is Rs {MIN_ORDER_PAISE // 100}")
            now = self.clock()
            wanted = {line["skuId"]: line["quantity"] for line in lines}
            for old in self.orders:
                recent = now - old["created_at"] < DUPLICATE_WINDOW_S
                if recent and old["status"] != "FAILED" and self._lines_key(old) == wanted:
                    if not confirm_duplicate:
                        raise StoreError(
                            "DUPLICATE_ORDER: probable duplicate of a recent order. Re-checkout "
                            "with confirmDuplicate=true only if the user confirms."
                        )
                    break
            address = self._address(self.cart_address_id)  # the cart's address, not the argument
            n = next(self._ids)
            order = {
                "order_id": f"IM-{n}",
                "paas_id": f"paas{n}",
                "transaction_id": f"txn{n}",
                "status": "PENDING_PAYMENT" if method == "UPI" else "CONFIRMED",
                "payment_status": "PENDING" if method == "UPI" else "COD",
                "payment_method": method,
                "created_at": now,
                "updated_at": now,
                "lines": lines,
                "item_total": item_total,
                "fees": fees,
                "total": to_pay,
                "address": address,
            }
            self.orders.append(order)
            self.cart = {}
            common = {
                "orderId": order["order_id"],
                "addressId": address["id"],
                "cartTotal": rupees(to_pay),
                "deliveryAddress": address["addressLine"],
                "deliveryLabel": address["addressTag"],
            }
            if method != "UPI":
                return {**common, "status": "CONFIRMED", "paymentMethod": "Cash"}
            return {
                **common,
                "transactionId": order["transaction_id"],
                "paasId": order["paas_id"],
                "upiIntentUrl": (
                    f"upi://pay?pa=demo@upi&pn=Demo%20Store&am={money(to_pay)}"
                    f"&tr={order['transaction_id']}&cu=INR"
                ),
                "bridgeUrl": f"https://demo-store.invalid/pay/{order['paas_id']}",
                "isQrFlow": bool(generate_upi_qr),
                "pollingIntervalInMs": 3000,
                "maxTimeToPollForInMs": UNPAID_EXPIRY_S * 1000,
                "paymentMethod": "UPI",
                "status": "PENDING_PAYMENT",
            }

    def _order_by_paas(self, paas_id: str) -> dict:
        for order in self.orders:
            if order["paas_id"] == paas_id:
                return order
        raise StoreError(f"Unknown paasId {paas_id!r}")

    def check_payment_status(self, paas_id: str, order_id: str | None = None) -> dict:
        with self._lock:
            self._expire()
            order = self._order_by_paas(paas_id)
            if order_id and order_id != order["order_id"]:
                raise StoreError("orderId does not match paasId")
            status = {"PENDING": "PENDING", "SUCCESS": "SUCCESS", "FAILED": "FAILED"}[
                order["payment_status"]
            ]
            data: dict[str, Any] = {
                "paasId": paas_id,
                "status": status,
                "terminal": status != "PENDING",
                "isTerminalSuccess": status == "SUCCESS",
                "isTerminalFailure": status == "FAILED",
                "orderId": order["order_id"],
                "orderStatus": order["status"],
                "cartTotal": rupees(order["total"]),
                "addressId": order["address"]["id"],
            }
            if status == "SUCCESS":
                data["confirmed"] = True
            return data

    def confirm_order(self, order_id: str, paas_id: str, transaction_id: str | None = None) -> dict:
        """The simulated UPI PIN step: completes a PENDING_PAYMENT order."""
        with self._lock:
            self._expire()
            order = self._order_by_paas(paas_id)
            if order_id != order["order_id"]:
                raise StoreError("orderId does not match paasId")
            if order["status"] == "PENDING_PAYMENT":
                order["status"] = "CONFIRMED"
                order["payment_status"] = "SUCCESS"
                order["updated_at"] = self.clock()
            result = "failed" if order["status"] == "FAILED" else "success"
            return {
                "orderId": order["order_id"],
                "paasId": paas_id,
                "orderStatus": order["status"],
                "result": result,
            }

    def get_orders(
        self, count: int = 10, order_type: str | None = None, active_only: bool = False
    ) -> dict:
        with self._lock:
            self._expire()
            if order_type not in (None, "INSTAMART"):
                return {"orders": [], "hasMore": False}
            rows = []
            for order in reversed(self.orders):
                active = order["status"] in ("PENDING_PAYMENT", "CONFIRMED")
                if active_only and not active:
                    continue
                fees = order["fees"]
                rows.append(
                    {
                        "orderId": order["order_id"],
                        "status": order["status"],
                        "createdAt": _iso(order["created_at"]),
                        "updatedAt": _iso(order["updated_at"]),
                        "itemCount": sum(line["quantity"] for line in order["lines"]),
                        "totalAmount": rupees(order["total"]),
                        "deliveryAddress": {
                            "id": order["address"]["id"],
                            "addressLine": order["address"]["addressLine"],
                            "phoneNumber": order["address"]["phoneNumber"],
                        },
                        "paymentMethod": order["payment_method"],
                        "orderType": "INSTAMART",
                        "isActive": active,
                        "currentStatus": order["status"],
                        "historyStatus": order["status"],
                        "storeName": "Demo store",
                        "items": [
                            {
                                "name": f"{line['itemName']} {line['itemVariant']}",
                                "quantity": line["quantity"],
                                "itemId": line["skuId"],
                            }
                            for line in order["lines"]
                        ],
                        "billDetails": {
                            "itemTotal": rupees(order["item_total"]),
                            "deliveryFee": rupees(
                                fees.get("Delivery fee", 0) + fees.get("Surge fee", 0)
                            ),
                            "packagingFee": rupees(fees.get("Handling fee", 0)),
                            "grandTotal": rupees(order["total"]),
                        },
                        "paymentStatus": order["payment_status"],
                    }
                )
            return {"orders": rows[:count], "hasMore": len(rows) > count}


def _iso(ts: float) -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(ts))


__all__ = ["HAMPER_SKU", "DemoStore", "StoreError", "money", "rupees"]
