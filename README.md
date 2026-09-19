# QuoteCheck

**Supplier quotes → extraction → normalization → comparison → exceptions → JSON**

QuoteCheck is an Apify Actor that compares supplier quotes and turns messy quote data into a structured, machine-readable comparison.

It supports two input modes:

1. **PDF quotes** - upload text-based supplier quote PDFs.
2. **Structured JSON** - send already-extracted quote data directly for API and AI-agent workflows.

QuoteCheck detects differences in:

- prices
- quantities
- missing items
- supplier totals
- commercial terms

It can also calculate **like-for-like normalized totals** when quoted quantities differ, and flags comparisons that require human review.

## MVP
QuoteCheck accepts two input modes:

1. **PDF quotes**: upload 2–10 text-based supplier quote PDFs.
2. **Structured JSON**: send normalized quotes directly for API/agent workflows.

For PDF input, the Actor extracts text, detects common quote line-item rows, normalizes quantities and prices, matches equivalent items across suppliers, calculates totals, and returns machine-readable JSON.

### PDF flow

`PDF Quote A + PDF Quote B → extract → normalize → match → compare → JSON`

The current MVP supports **text-based PDFs**. Scanned/image-only PDFs are detected and reported as requiring OCR rather than silently producing bad data.

## Output

Returns:

- matched item groups
- supplier-level quantities and prices
- missing items/suppliers
- quantity differences
- price differences
- calculated supplier totals
- lowest comparable total
- extraction warnings
- review_required flag
- extraction metadata

## Structured JSON example

```json
{
  "quotes": [
    {
      "supplier": "Supplier A",
      "items": [
        {"description": "Dell Latitude 7450", "quantity": 10, "unit_price": 1150}
      ]
    },
    {
      "supplier": "Supplier B",
      "items": [
        {"description": "Latitude 7450 Laptop", "quantity": 10, "unit_price": 1095}
      ]
    }
  ]
}
```

## Design principle

QuoteCheck performs one business operation and returns predictable JSON that another automation or AI agent can consume. It does not scrape websites or generate a narrative report.

## Limitations

- PDF extraction relies on text embedded in the PDF.
- Scanned PDFs require an OCR layer, which is the next logical upgrade.
- Table layouts vary, so extraction warnings are surfaced and `review_required` is set when the result needs human checking.
