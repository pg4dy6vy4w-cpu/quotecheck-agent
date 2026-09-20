# Supplier Quote Comparison: PDF & JSON | QuoteCheck

Compare supplier quotes and return structured price, quantity and exception data for purchasing workflows and AI agents.

## Try a comparison

Use structured JSON for a first run. Leave **Supplier quote file stores** empty, then use the prefilled **Structured supplier quotes** field. When calling the API, send the complete object below.

```json
{
  "quotes": [
    {
      "supplier": "Supplier A",
      "currency": "GBP",
      "items": [
        {
          "description": "Dell Latitude 7450",
          "quantity": 10,
          "unit_price": 1150
        }
      ]
    },
    {
      "supplier": "Supplier B",
      "currency": "GBP",
      "items": [
        {
          "description": "Dell Latitude 7450",
          "quantity": 10,
          "unit_price": 1095
        }
      ]
    }
  ]
}
```

The sample compares ten identical laptops in GBP. Supplier A totals £11,500; Supplier B totals £10,950, a £550 difference.

Expected result excerpt:

```json
{
  "status": "comparable",
  "review_required": false,
  "supplier_totals": {
    "Supplier A": 11500.0,
    "Supplier B": 10950.0
  },
  "totals_comparable": true,
  "lowest_total_supplier": "Supplier B"
}
```

The full result also includes matched items, price differences, decision flags and other comparison fields. The example input and full expected output are in `examples/input.json` and `examples/output.json` in the source repository. This example was verified locally; it is not a claim of a live customer run.

## Compare PDF quotes

Store at least two text-based quote PDFs in Apify Key-Value Stores and select those stores in **Supplier quote file stores**. This mode takes priority over structured JSON when both are supplied.

QuoteCheck extracts embedded text and attempts to identify line items and commercial terms. Scanned PDFs without text are rejected and require OCR before use. Layout support is limited; inspect extraction results before relying on a new document layout.

## What the result contains

- Matched items and suppliers missing from each group.
- Unit-price and quantity differences.
- Supplier totals and a lowest-total supplier when comparison checks pass.
- Like-for-like estimates for differing quantities when sufficient data exists.
- Commercial-term differences and validation warnings.
- `review_required` and `decision_flags` for downstream routing.

## Input and interpretation

Use distinct supplier names, positive quantities and finite, non-negative unit prices. Include an explicit `currency` such as `GBP`, `EUR` or `USD` for every structured quote. Currency conversion is not performed. Numeric-only inputs without currency assume a common currency and disclose that assumption.

Totals that differ from the item arithmetic require review, including legitimate adjustments for tax, shipping or discounts. Resolve warnings before choosing a supplier. `review_required: false` means the implemented checks passed; it does not guarantee complete PDF extraction or commercial equivalence.

Quantity normalization applies each quoted unit price to the largest quoted quantity for that item. It is an estimate, not a binding supplier offer or a volume-discount calculation.

## API and AI-agent workflows

Input: supplier quotes as JSON or text-based PDFs in Apify storage.

Output: structured JSON in the default dataset and the `OUTPUT` key-value record.

Use this operation in supplier quote comparison, RFQ comparison, procurement automation and purchasing workflows. Eligible Actors can be accessed through Apify's existing MCP server; clients must configure their own Apify access. QuoteCheck does not need a separate MCP server.
