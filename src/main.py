import asyncio
import json
import re
from difflib import SequenceMatcher
from decimal import Decimal, InvalidOperation
from io import BytesIO
from urllib.parse import unquote, urlparse

from apify import Actor
from pypdf import PdfReader


def norm_text(value):
    text = str(value or "").lower()
    text = re.sub(r"[^a-z0-9]+", " ", text)
    text = re.sub(r"\bdocking station\b", "dock", text)
    text = re.sub(r"\bdocking\b", "dock", text)
    text = re.sub(r"\blaptop computer\b", "laptop", text)
    text = re.sub(r"\bnotebook computer\b", "notebook", text)
    return re.sub(r"\s+", " ", text).strip()


def money(value):
    if value is None or value == "":
        return None
    try:
        cleaned = (
            str(value)
            .replace(",", "")
            .replace("£", "")
            .replace("$", "")
            .replace("€", "")
            .strip()
        )
        return float(Decimal(cleaned))
    except (InvalidOperation, ValueError, TypeError):
        return None


def number(value):
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def item_key(item):
    return norm_text(
        item.get("description")
        or item.get("name")
        or item.get("sku")
        or item.get("item")
    )


def product_identifiers(text):
    tokens = set(norm_text(text).split())
    identifiers = set()
    for token in tokens:
        if re.search(r"[a-z]", token) and re.search(r"\d", token):
            identifiers.add(token)
        elif re.fullmatch(r"\d{4,}", token):
            identifiers.add(token)
    return identifiers


def product_modifiers(text):
    tokens = set(norm_text(text).split())
    return tokens & {
        "mini", "max", "pro", "plus", "ultra", "lite", "se",
        "xl", "large", "small", "compact", "extended",
    }


def critical_attributes(text):
    """Extract product attributes where a mismatch usually means a different SKU."""
    normalized = norm_text(text)
    attributes = {}

    patterns = {
        "screen_size": r"\b(\d{1,2}(?:\.\d)?)\s*(?:inch|in)\b",
        "ram": r"\b(\d{1,3})\s*gb\s*(?:ram|memory)?\b",
        "storage": r"\b(\d{3,5})\s*(?:gb|tb)\s*(?:ssd|hdd|storage)?\b",
        "wattage": r"\b(\d{2,4})\s*w\b",
        "capacity": r"\b(\d{1,6})\s*(?:mah|ml|l|kg|g)\b",
        "generation": r"\b(?:gen|generation)\s*(\d{1,2})\b",
    }
    for name, pattern in patterns.items():
        match = re.search(pattern, normalized)
        if match:
            attributes[name] = match.group(1)

    # Common model-number families such as Latitude 5450/5440 and WD19/WD19S
    model_match = re.search(
        r"\b([a-z]+\s*\d{2,5}[a-z]?)\b",
        normalized,
    )
    if model_match:
        attributes["model"] = re.sub(r"\s+", " ", model_match.group(1)).strip()

    return attributes


def items_compatible(item_a, item_b):
    sku_a = norm_text(item_a.get("sku"))
    sku_b = norm_text(item_b.get("sku"))

    if sku_a and sku_b:
        return sku_a == sku_b
    if sku_a and not sku_b:
        # A supplier without a SKU can still match a SKU-bearing item only
        # when the description contains the same explicit model identifier.
        ids_a = product_identifiers(item_a.get("description") or item_a.get("name") or "")
        ids_b = product_identifiers(item_b.get("description") or item_b.get("name") or "")
        if ids_a and ids_b and ids_a & ids_b:
            pass
        else:
            return False

    text_a = item_a.get("description") or item_a.get("name") or ""
    text_b = item_b.get("description") or item_b.get("name") or ""
    ids_a = product_identifiers(text_a)
    ids_b = product_identifiers(text_b)

    if ids_a and ids_b and ids_a.isdisjoint(ids_b):
        return False

    modifiers_a = product_modifiers(text_a)
    modifiers_b = product_modifiers(text_b)
    if modifiers_a ^ modifiers_b:
        return False

    attrs_a = critical_attributes(text_a)
    attrs_b = critical_attributes(text_b)
    for field in set(attrs_a) & set(attrs_b):
        if attrs_a[field] != attrs_b[field]:
            return False

    return True


def similarity(a, b, item_a=None, item_b=None):
    if not a or not b:
        return 0.0
    if item_a is not None and item_b is not None and not items_compatible(item_a, item_b):
        return 0.0
    if a == b:
        return 1.0

    char_score = SequenceMatcher(None, a, b).ratio()
    a_tokens = set(a.split())
    b_tokens = set(b.split())
    if not a_tokens or not b_tokens:
        return char_score

    overlap = len(a_tokens & b_tokens) / len(a_tokens | b_tokens)
    containment = len(a_tokens & b_tokens) / min(len(a_tokens), len(b_tokens))

    a_numbers = set(re.findall(r"\b\d+[a-z]?\b", a))
    b_numbers = set(re.findall(r"\b\d+[a-z]?\b", b))
    shared_numbers = a_numbers & b_numbers

    score = char_score * 0.45 + overlap * 0.25 + containment * 0.30

    if shared_numbers:
        score = max(score, 0.85)

    # Shared explicit SKU/model identifiers are stronger evidence than
    # generic wording similarity.
    if item_a is not None and item_b is not None:
        sku_a = norm_text(item_a.get("sku"))
        sku_b = norm_text(item_b.get("sku"))
        if sku_a and sku_b and sku_a == sku_b:
            score = max(score, 0.99)

        ids_a = product_identifiers(item_a.get("description") or item_a.get("name") or "")
        ids_b = product_identifiers(item_b.get("description") or item_b.get("name") or "")
        if ids_a & ids_b:
            score = max(score, 0.92)

    return min(score, 1.0)


def match_items(quotes, threshold=0.72):
    """
    Build conservative one-to-one matches across supplier quotes.

    Candidate pairs are scored first, then selected globally from highest
    confidence downward. An item from a supplier can only belong to one group,
    which prevents duplicate matches and makes the result deterministic.
    """
    suppliers = [
        str(q.get("supplier") or q.get("vendor") or f"Supplier {i + 1}")
        for i, q in enumerate(quotes)
    ]

    entries = []
    for quote_index, quote in enumerate(quotes):
        for item_index, raw in enumerate(quote.get("items") or []):
            item = dict(raw) if isinstance(raw, dict) else {"description": str(raw)}
            key = item_key(item)
            if key:
                entries.append({
                    "quote_index": quote_index,
                    "item_index": item_index,
                    "supplier": suppliers[quote_index],
                    "item": item,
                    "key": key,
                })

    # Generate all compatible cross-supplier candidates.
    candidates = []
    for left_index, left in enumerate(entries):
        for right_index in range(left_index + 1, len(entries)):
            right = entries[right_index]
            if left["supplier"] == right["supplier"]:
                continue
            score = similarity(
                left["key"], right["key"], left["item"], right["item"]
            )
            if score >= threshold:
                candidates.append((score, left_index, right_index))

    # Highest-confidence pairs claim their items first.
    candidates.sort(key=lambda x: (-x[0], x[1], x[2]))
    matched_entry_ids = set()
    pair_groups = []

    for score, left_index, right_index in candidates:
        if left_index in matched_entry_ids or right_index in matched_entry_ids:
            continue
        left = entries[left_index]
        right = entries[right_index]
        matched_entry_ids.update({left_index, right_index})
        pair_groups.append({
            "entries": [left, right],
            "canonical_key": left["key"] if len(left["key"]) >= len(right["key"]) else right["key"],
            "canonical_label": (
                left["item"].get("description")
                or left["item"].get("name")
                or right["item"].get("description")
                or right["item"].get("name")
            ),
        })

    # Add unmatched entries as singleton groups. For >2 suppliers, attach a
    # third item only if it is compatible with an existing group and comes from
    # a supplier not already represented in that group.
    singleton_entries = [
        entry for index, entry in enumerate(entries)
        if index not in matched_entry_ids
    ]

    for entry in singleton_entries:
        best_group = None
        best_score = 0.0
        for group in pair_groups:
            if any(e["supplier"] == entry["supplier"] for e in group["entries"]):
                continue
            scores = [
                similarity(entry["key"], existing["key"], entry["item"], existing["item"])
                for existing in group["entries"]
            ]
            score = max(scores) if scores else 0.0
            if score > best_score:
                best_score = score
                best_group = group
        if best_group is not None and best_score >= threshold:
            best_group["entries"].append(entry)
            matched_entry_ids.add(entries.index(entry))
        else:
            pair_groups.append({
                "entries": [entry],
                "canonical_key": entry["key"],
                "canonical_label": (
                    entry["item"].get("description")
                    or entry["item"].get("name")
                    or entry["key"]
                ),
            })

    return pair_groups

def analyse_group(group, supplier_names):
    entries = group["entries"]
    by_supplier = {}
    for entry in entries:
        item = entry["item"]
        qty = number(item.get("quantity", 1))
        unit = money(item.get("unit_price", item.get("unitPrice", item.get("price"))))
        total = money(item.get("total", item.get("line_total", item.get("lineTotal"))))
        if total is None and qty is not None and unit is not None:
            total = round(qty * unit, 2)
        by_supplier[entry["supplier"]] = {
            "supplier": entry["supplier"],
            "description": item.get("description") or item.get("name"),
            "sku": item.get("sku"),
            "quantity": qty,
            "unit_price": unit,
            "total": total,
            "quote_index": entry["quote_index"],
            "item_index": entry["item_index"],
        }

    warnings = []
    suppliers_present = set(by_supplier)
    missing = sorted(set(supplier_names) - suppliers_present)

    quantities = [x["quantity"] for x in by_supplier.values() if x["quantity"] is not None]
    units = [x["unit_price"] for x in by_supplier.values() if x["unit_price"] is not None]

    if len(set(quantities)) > 1:
        warnings.append({
            "type": "quantity_difference",
            "values": {s: x["quantity"] for s, x in by_supplier.items() if x["quantity"] is not None},
        })

    if len(units) > 1:
        warnings.append({
            "type": "price_difference",
            "values": {s: x["unit_price"] for s, x in by_supplier.items() if x["unit_price"] is not None},
        })

    return {
        "canonical_item": group["canonical_label"],
        "suppliers": list(by_supplier.values()),
        "missing_suppliers": missing,
        "warnings": warnings,
        "match_confidence": round(
            min(
                similarity(
                    entry["key"],
                    other["key"],
                    entry["item"],
                    other["item"],
                )
                for i, entry in enumerate(entries)
                for j, other in enumerate(entries)
                if i < j
            ) if len(entries) > 1 else 0.0,
            3,
        ),
    }


def compare_quotes(quotes):
    suppliers = [
        str(q.get("supplier") or q.get("vendor") or f"Supplier {i + 1}")
        for i, q in enumerate(quotes)
    ]
    groups = match_items(quotes)

    matched = []
    missing = []
    price_differences = []
    quantity_differences = []

    for group in groups:
        result = analyse_group(group, suppliers)
        if len(result["suppliers"]) >= 2:
            matched.append(result)
        else:
            missing.append(result)
        for warning in result["warnings"]:
            if warning["type"] == "price_difference":
                price_differences.append({
                    "canonical_item": result["canonical_item"],
                    "values": warning["values"],
                })
            elif warning["type"] == "quantity_difference":
                quantity_differences.append({
                    "canonical_item": result["canonical_item"],
                    "values": warning["values"],
                })

    # Raw totals are the totals exactly as quoted by each supplier.
    totals = {}
    for quote in quotes:
        supplier = str(quote.get("supplier") or quote.get("vendor") or "Unknown")
        total = money(quote.get("total"))
        if total is None:
            total = 0.0
            complete = True
            for item in quote.get("items") or []:
                if not isinstance(item, dict):
                    complete = False
                    break
                qty = number(item.get("quantity", 1))
                unit = money(item.get("unit_price", item.get("unitPrice", item.get("price"))))
                line = money(item.get("total", item.get("line_total", item.get("lineTotal"))))
                if line is None and qty is not None and unit is not None:
                    line = qty * unit
                if line is None:
                    complete = False
                    break
                total += line
            if not complete:
                total = None
        totals[supplier] = round(total, 2) if total is not None else None

    comparable_totals = {k: v for k, v in totals.items() if v is not None}

    # A raw total is only directly comparable when every matched item has the
    # same quantity across suppliers and no item is missing from a supplier.
    quantities_differ = bool(quantity_differences)
    directly_comparable = (
        not missing
        and not quantities_differ
        and len(comparable_totals) == len(suppliers)
    )
    lowest = (
        min(comparable_totals, key=comparable_totals.get)
        if directly_comparable and comparable_totals
        else None
    )

    # When quantities differ, calculate a like-for-like estimate using the
    # highest quantity quoted for each matched item. This does not invent a
    # supplier price; it applies each supplier's quoted unit price to the
    # common quantity. We only expose this when every item is present and has
    # both quantity and unit price for every supplier.
    normalized_totals = {}
    normalization_possible = not missing and bool(groups)

    if normalization_possible:
        for supplier in suppliers:
            normalized_totals[supplier] = 0.0

        for group in groups:
            by_supplier = {
                entry["supplier"]: entry["item"]
                for entry in group["entries"]
            }
            if set(by_supplier) != set(suppliers):
                normalization_possible = False
                break

            quantities = []
            for supplier in suppliers:
                item = by_supplier[supplier]
                qty = number(item.get("quantity", 1))
                unit = money(
                    item.get(
                        "unit_price",
                        item.get("unitPrice", item.get("price")),
                    )
                )
                if qty is None or unit is None:
                    normalization_possible = False
                    break
                quantities.append(qty)

            if not normalization_possible:
                break

            target_quantity = max(quantities)
            for supplier in suppliers:
                item = by_supplier[supplier]
                unit = money(
                    item.get(
                        "unit_price",
                        item.get("unitPrice", item.get("price")),
                    )
                )
                normalized_totals[supplier] += target_quantity * unit

    if normalization_possible:
        normalized_totals = {
            supplier: round(total, 2)
            for supplier, total in normalized_totals.items()
        }
        lowest_normalized = min(
            normalized_totals,
            key=normalized_totals.get,
        )
    else:
        normalized_totals = {}
        lowest_normalized = None

    if directly_comparable:
        comparison_note = "Supplier totals are directly comparable because quoted quantities match."
    elif normalization_possible:
        comparison_note = (
            "Quoted totals are not directly comparable because quantities differ. "
            "Like-for-like totals are estimated using the highest quoted quantity "
            "for each matched item and each supplier's quoted unit price."
        )
    elif missing:
        comparison_note = (
            "Quoted totals are not directly comparable because one or more items "
            "are missing from at least one supplier quote."
        )
    else:
        comparison_note = (
            "Quoted totals are not directly comparable because the available "
            "item quantities or prices are incomplete."
        )

    commercial_terms_differences = []
    term_fields = (
        "valid_until",
        "delivery",
        "payment_terms",
        "warranty",
        "shipping",
        "discount",
        "tax",
    )
    for field in term_fields:
        values = {
            str(q.get("supplier") or q.get("vendor") or f"Supplier {i + 1}"):
                (q.get("commercial_terms") or {}).get(field)
            for i, q in enumerate(quotes)
        }
        present = {supplier: value for supplier, value in values.items() if value}
        if len(present) >= 2 and len(set(present.values())) > 1:
            commercial_terms_differences.append({
                "type": field,
                "values": present,
            })

    result = {
        "suppliers": suppliers,
        "supplier_totals": comparable_totals,
        "totals_comparable": directly_comparable,
        "lowest_total_supplier": lowest,
        "matched_items": matched,
        "unmatched_items": missing,
        "price_differences": price_differences,
        "quantity_differences": quantity_differences,
        "commercial_terms_differences": commercial_terms_differences,
        "warnings": [
            {"type": "supplier_total_not_calculable", "supplier": k}
            for k, v in totals.items()
            if v is None
        ],
        "comparison_note": comparison_note,
    }

    if normalized_totals:
        result["normalized_supplier_totals"] = normalized_totals
        result["lowest_normalized_total_supplier"] = lowest_normalized
        result["normalized_savings"] = round(
            max(normalized_totals.values()) - min(normalized_totals.values()), 2
        ) if len(normalized_totals) > 1 else 0.0

    # Compact, deterministic flags make the output easier for an AI agent to
    # consume without having to infer issues from the detailed arrays.
    decision_flags = []
    if missing:
        decision_flags.append({
            "type": "missing_items",
            "severity": "high",
            "message": "One or more matched item groups are missing from a supplier quote.",
        })
    if quantity_differences:
        decision_flags.append({
            "type": "quantity_mismatch",
            "severity": "high",
            "message": "At least one matched item has different quoted quantities.",
        })
    if price_differences:
        decision_flags.append({
            "type": "price_variance",
            "severity": "info",
            "message": "At least one matched item has different unit prices.",
        })
    if commercial_terms_differences:
        decision_flags.append({
            "type": "commercial_terms_difference",
            "severity": "medium",
            "message": "Supplier quotes contain different commercial terms that may affect comparability.",
            "terms": [item["type"] for item in commercial_terms_differences],
        })
    if result["warnings"]:
        decision_flags.append({
            "type": "incomplete_totals",
            "severity": "high",
            "message": "At least one supplier total could not be calculated.",
        })

    low_confidence = [
        item for item in matched
        if item.get("match_confidence", 1) < 0.75
    ]
    if low_confidence:
        decision_flags.append({
            "type": "low_match_confidence",
            "severity": "medium",
            "message": "One or more item matches have confidence below 0.75 and should be reviewed.",
            "items": [item["canonical_item"] for item in low_confidence],
        })

    result["decision_flags"] = decision_flags

    return result


async def download_bytes(source):
    # The fileupload editor gives us an Apify Key-Value Store record URL.
    # Read the record through the Actor's authenticated Apify API client rather
    # than downloading the URL directly. This works for temporary/private KVS.
    parsed = urlparse(source)
    parts = [unquote(part) for part in parsed.path.split("/") if part]
    try:
        store_index = parts.index("key-value-stores")
        record_index = parts.index("records", store_index)
        store_id = parts[store_index + 1]
        record_key = parts[record_index + 1]
    except (ValueError, IndexError):
        raise ValueError(f"Unsupported quote PDF source: {source}")

    record = await Actor.apify_client.key_value_store(store_id).get_record_as_bytes(record_key)
    if record is None:
        raise ValueError(f"Could not read uploaded PDF record: {record_key}")

    if isinstance(record, bytes):
        return record
    if isinstance(record, bytearray):
        return bytes(record)
    if isinstance(record, dict) and isinstance(record.get("value"), (bytes, bytearray)):
        return bytes(record["value"])

    raise ValueError(
        f"Uploaded PDF record did not return binary data (type={type(record).__name__})."
    )


def extract_pdf_text(pdf_bytes):
    reader = PdfReader(BytesIO(pdf_bytes))
    page_texts = [page.extract_text() or "" for page in reader.pages]
    return "\n".join(page_texts).strip(), page_texts


def evidence_for(value, page_texts):
    if not value:
        return None
    target = norm_text(value)
    for page_number, page_text in enumerate(page_texts, 1):
        lines = [re.sub(r"\s+", " ", line).strip() for line in page_text.splitlines() if line.strip()]
        for i, line in enumerate(lines):
            if target and target in norm_text(line):
                context = " ".join(lines[max(0, i - 1):min(len(lines), i + 2)])
                return {
                    "page": page_number,
                    "snippet": context[:500],
                }
    return None


def extract_labeled_value(text, patterns):
    for pattern in patterns:
        match = re.search(pattern, text, re.I | re.M)
        if match:
            value = match.group(1).strip(" \t:|-")
            if value:
                return value
    return None


def extract_commercial_terms(text):
    return {
        "quote_reference": extract_labeled_value(text, [
            r"(?im)^\s*(?:quote|quotation|proposal)\s*(?:reference|ref|number|no\.?)[ \t:#-]+(.+?)\s*$",
        ]),
        "quote_date": extract_labeled_value(text, [
            r"(?im)^\s*(?:quote|quotation|proposal)\s*date[ \t:#-]+(.+?)\s*$",
            r"(?im)^\s*date[ \t:#-]+(.+?)\s*$",
        ]),
        "valid_until": extract_labeled_value(text, [
            r"(?im)^\s*(?:valid until|quote valid until|valid through)[ \t:#-]+(.+?)\s*$",
            r"(?im)^\s*(?:valid for)[ \t:#-]+(.+?)\s*$",
        ]),
        "delivery": extract_labeled_value(text, [
            r"(?im)^\s*(?:delivery|delivery time|delivery date|lead time)[ \t:#-]+(.+?)\s*$",
        ]),
        "payment_terms": extract_labeled_value(text, [
            r"(?im)^\s*(?:payment terms|payment)[ \t:#-]+(.+?)\s*$",
        ]),
        "warranty": extract_labeled_value(text, [
            r"(?im)^\s*(?:warranty|guarantee)[ \t:#-]+(.+?)\s*$",
        ]),
        "shipping": extract_labeled_value(text, [
            r"(?im)^\s*(?:shipping|freight|delivery charge|shipping charge)[ \t:#-]+(.+?)\s*$",
        ]),
        "discount": extract_labeled_value(text, [
            r"(?im)^\s*(?:discount|discounts)[ \t:#-]+(.+?)\s*$",
        ]),
        "tax": extract_labeled_value(text, [
            r"(?im)^\s*(?:vat|tax|sales tax)[ \t:#-]+(.+?)\s*$",
        ]),
    }


def infer_supplier(text, source, index):
    patterns = [
        r"(?im)^\s*(?:supplier|vendor|seller|company|from)\s*[:#-]\s*(.+?)\s*$",
        r"(?im)^\s*(?:supplier|vendor|seller|company|from)\s+(.+?)\s*$",
    ]
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            candidate = match.group(1).strip()
            if 1 < len(candidate) < 100:
                return candidate

    filename = source.rsplit("/", 1)[-1].split("?", 1)[0]
    filename = re.sub(r"\.(pdf)$", "", filename, flags=re.I)
    filename = re.sub(r"[_-]+", " ", filename).strip()
    return filename or f"Supplier {index + 1}"


def parse_money_tokens(line):
    matches = re.findall(r"(?:£|\$|€)?\s*\d{1,3}(?:,\d{3})*(?:\.\d{2})|(?:£|\$|€)?\s*\d+(?:\.\d{2})", line)
    return [money(x) for x in matches if money(x) is not None]


def parse_pdf_quote(text, source, index, page_texts=None):
    lines = [re.sub(r"\s+", " ", line).strip() for line in text.splitlines()]
    lines = [line for line in lines if line]

    supplier = infer_supplier(text, source, index)
    items = []
    extraction_warnings = []

    # Handles common text-extracted table rows such as:
    # "Dell Latitude 7450 10 £1,150 £11,500"
    # "Latitude 7450 Laptop | 10 | 1095 | 10950"
    row_pattern = re.compile(
        r"^(?P<desc>.+?)\s+(?P<qty>\d+(?:\.\d+)?)\s+"
        r"(?P<unit>(?:£|\$|€)?\s*[\d,]+(?:\.\d{1,2})?)"
        r"(?:\s+(?P<total>(?:£|\$|€)?\s*[\d,]+(?:\.\d{1,2})?))?\s*$",
        re.I,
    )

    for line in lines:
        lowered = line.lower()
        if any(
            header in lowered
            for header in (
                "description quantity",
                "item quantity",
                "unit price",
                "subtotal",
                "grand total",
                "quote total",
                "invoice total",
                "terms and conditions",
            )
        ):
            continue

        match = row_pattern.match(line)
        if not match:
            continue

        description = match.group("desc").strip(" |-:")
        if len(description) < 2 or len(description) > 180:
            continue

        qty = number(match.group("qty"))
        unit_price = money(match.group("unit"))
        total = money(match.group("total"))

        if unit_price is None:
            continue

        items.append({
            "description": description,
            "quantity": qty,
            "unit_price": unit_price,
            "total": total,
        })

    # pypdf often extracts table columns as separate lines, for example:
    # Description / Quantity / Unit Price / Total / Dell Latitude 7450 / 10 /
    # £1,150.00 / £11,500.00. Reconstruct those four-column rows before
    # falling back to looser parsing.
    if not items:
        ignored = {
            "supplier quote",
            "description",
            "quantity",
            "unit price",
            "total",
            "grand total",
            "quote total",
            "invoice total",
            "amount due",
        }
        i = 0
        while i < len(lines):
            candidate = lines[i].strip()
            lowered = candidate.lower()

            if lowered in ignored or lowered.startswith("quote reference"):
                i += 1
                continue

            if i + 2 < len(lines):
                qty = number(lines[i + 1])
                prices = parse_money_tokens(lines[i + 2])
                if qty is not None and len(prices) >= 1:
                    total = None
                    unit_price = prices[0]
                    consumed = 3

                    if i + 3 < len(lines):
                        next_prices = parse_money_tokens(lines[i + 3])
                        if len(next_prices) == 1 and re.search(r"[£$€]", lines[i + 3]):
                            total = next_prices[0]
                            consumed = 4

                    if (
                        candidate
                        and not re.search(
                            r"(?i)^(grand\s+total|quote\s+total|total\s+due|amount\s+due)$",
                            candidate,
                        )
                    ):
                        items.append({
                            "description": candidate,
                            "quantity": qty,
                            "unit_price": unit_price,
                            "total": total,
                        })
                        i += consumed
                        continue

            i += 1

    # A fallback for PDFs whose extracted table columns are flattened:
    # identify lines containing at least two currency/number values and use
    # the final numeric fields as price data.
    if not items:
        for line in lines:
            values = parse_money_tokens(line)
            if len(values) < 2:
                continue
            numbers = re.findall(r"\b\d+(?:\.\d+)?\b", line)
            if len(numbers) < 3:
                continue

            qty = number(numbers[-3])
            unit_price = values[-2]
            total = values[-1]
            description = re.sub(r"(?:£|\$|€)?\s*[\d,]+(?:\.\d{1,2})?", "", line)
            description = re.sub(r"\s+", " ", description).strip(" |-:")
            if description and qty is not None and unit_price is not None:
                items.append({
                    "description": description,
                    "quantity": qty,
                    "unit_price": unit_price,
                    "total": total,
                })

    if not items:
        extraction_warnings.append({
            "type": "no_line_items_detected",
            "message": "No quote line items could be extracted from PDF text. The PDF may use a layout or scan that requires OCR.",
        })

    total_match = re.search(
        r"(?im)(?:grand\s+total|quote\s+total|total\s+due|amount\s+due)\s*[:\-]?\s*(?:£|\$|€)?\s*([\d,]+(?:\.\d{1,2})?)",
        text,
    )
    quote_total = money(total_match.group(1)) if total_match else None

    commercial_terms = extract_commercial_terms(text)
    evidence = {}
    pages = page_texts or [text]

    supplier_evidence = evidence_for(supplier, pages)
    if supplier_evidence:
        evidence["supplier"] = supplier_evidence

    for field, value in commercial_terms.items():
        if value:
            field_evidence = evidence_for(value, pages)
            if field_evidence:
                evidence[field] = field_evidence

    item_evidence = []
    for item in items:
        item_ev = evidence_for(item.get("description"), pages)
        if item_ev:
            item_evidence.append({
                "description": item.get("description"),
                **item_ev,
            })
    if item_evidence:
        evidence["items"] = item_evidence

    return {
        "supplier": supplier,
        "items": items,
        "total": quote_total,
        "commercial_terms": {
            key: value for key, value in commercial_terms.items() if value
        },
        "_evidence": evidence,
        "_source": source,
        "_extraction_warnings": extraction_warnings,
    }


async def parse_pdf_sources(sources):
    quotes = []
    extraction = []

    for index, source in enumerate(sources):
        if not isinstance(source, str) or not source.strip():
            continue

        Actor.log.info(f"Extracting quote PDF {index + 1}/{len(sources)}")
        pdf_bytes = await download_bytes(source)
        text, page_texts = extract_pdf_text(pdf_bytes)

        if not text:
            raise ValueError(
                f"PDF {index + 1} contains no extractable text. "
                "This MVP supports text-based PDFs; scanned PDFs require OCR."
            )

        quote = parse_pdf_quote(text, source, index, page_texts)
        extraction.append({
            "source": source,
            "supplier": quote["supplier"],
            "pages_text_chars": len(text),
            "items_detected": len(quote["items"]),
            "commercial_terms_detected": len(quote.get("commercial_terms") or {}),
            "warnings": quote.pop("_extraction_warnings"),
        })
        quotes.append(quote)

    if len(quotes) < 2:
        raise ValueError("Provide at least two quote PDFs.")

    return quotes, extraction


async def parse_pdf_kvs_sources(store_ids):
    quotes = []
    extraction = []
    pdf_records = []

    for store_id in store_ids:
        if not isinstance(store_id, str) or not store_id.strip():
            continue

        store = Actor.apify_client.key_value_store(store_id)
        keys = store.iterate_keys(limit=1000)

        async for metadata in keys:
            key = getattr(metadata, "key", None)
            content_type = getattr(metadata, "content_type", None)
            if isinstance(metadata, dict):
                key = metadata.get("key", key)
                content_type = metadata.get("contentType", metadata.get("content_type", content_type))

            if not key:
                continue

            key_text = str(key)
            if not (
                key_text.lower().endswith(".pdf")
                or str(content_type or "").lower().split(";")[0] == "application/pdf"
            ):
                continue

            pdf_records.append((store_id, key_text))

    if len(pdf_records) < 2:
        raise ValueError(
            "Selected quote storage must contain at least two PDF records. "
            "Select the Key-Value Stores containing your supplier quote PDFs."
        )

    for index, (store_id, key) in enumerate(pdf_records):
        Actor.log.info(
            f"Extracting quote PDF {index + 1}/{len(pdf_records)} from {store_id}/{key}"
        )
        record = await Actor.apify_client.key_value_store(store_id).get_record_as_bytes(key)
        if record is None:
            raise ValueError(f"Could not read PDF record: {store_id}/{key}")

        if isinstance(record, bytearray):
            record = bytes(record)
        elif isinstance(record, dict) and isinstance(record.get("value"), (bytes, bytearray)):
            record = bytes(record["value"])

        if not isinstance(record, bytes):
            raise ValueError(
                f"PDF record did not return binary data: {store_id}/{key}"
            )

        text, page_texts = extract_pdf_text(record)
        if not text:
            raise ValueError(
                f"PDF record {key} contains no extractable text. "
                "This MVP supports text-based PDFs; scanned PDFs require OCR."
            )

        source = f"{store_id}/{key}"
        quote = parse_pdf_quote(text, source, index, page_texts)
        extraction.append({
            "source": source,
            "supplier": quote["supplier"],
            "pages_text_chars": len(text),
            "items_detected": len(quote["items"]),
            "commercial_terms_detected": len(quote.get("commercial_terms") or {}),
            "warnings": quote.pop("_extraction_warnings"),
        })
        quotes.append(quote)

    return quotes, extraction


async def main():
    async with Actor:
        raw = await Actor.get_input() or {}

        extraction = []
        if raw.get("quote_stores"):
            quotes, extraction = await parse_pdf_kvs_sources(raw["quote_stores"])
        elif raw.get("quote_pdfs"):
            # Backward-compatible support for direct file-upload URLs.
            quotes, extraction = await parse_pdf_sources(raw["quote_pdfs"])
        else:
            quotes = raw.get("quotes")

        if not isinstance(quotes, list) or len(quotes) < 2:
            raise ValueError(
                "Input must contain either 'quote_stores' with at least two PDF records "
                "or a 'quotes' array with at least two structured quotes."
            )

        for quote in quotes:
            if not isinstance(quote, dict):
                raise ValueError("Each quote must be an object.")
            if not (quote.get("items") or []):
                raise ValueError("Each quote must contain an 'items' array.")

        result = compare_quotes(quotes)
        result["status"] = "review" if (
            result["unmatched_items"]
            or result["quantity_differences"]
            or result.get("commercial_terms_differences")
            or result["warnings"]
            or any(x.get("warnings") for x in extraction)
        ) else "comparable"

        result["review_required"] = result["status"] == "review"
        result["input_summary"] = {
            "quote_count": len(quotes),
            "item_count": sum(len(q.get("items") or []) for q in quotes),
            "input_mode": "pdf" if extraction else "json",
        }

        if extraction:
            result["extraction"] = extraction

        result["commercial_terms"] = {
            quote.get("supplier"): quote.get("commercial_terms", {})
            for quote in quotes
        }
        result["evidence"] = {
            quote.get("supplier"): quote.get("_evidence", {})
            for quote in quotes
            if quote.get("_evidence")
        }

        await Actor.push_data(result)
        await Actor.set_value(
            "OUTPUT",
            json.dumps(result, indent=2),
            content_type="application/json",
        )
        Actor.log.info(
            f"QuoteCheck completed: {len(quotes)} quotes, "
            f"{len(result['matched_items'])} matched item groups, "
            f"status={result['status']}"
        )


if __name__ == "__main__":
    asyncio.run(main())
