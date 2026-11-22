# Scoring Options

Scoring ranks the vendors found for each product. It reads saved results only, so it never costs credits and can be run as often as you like. Results are saved for your account, and you can download them as an Excel file.

## Which products

Scoring uses the same RFQ file, granularity, limit and positions settings as the search. Use the same values you searched with, or families will not match saved results. Families without saved results show up as warnings.

**Top N**
How many vendors to keep for each product.

## Hard filters

A vendor that fails a hard filter is moved to the Disqualified list with the reason.

- **Require trade assurance:** the vendor must have the Trade Assurance badge.
- **Minimum years:** the vendor must have been active at least this long.
- **Max price:** a price ceiling used when a product row has no max price of its own. A row's own max price always wins.
- **Strict MOQ:** reject vendors whose minimum order is above the quantity you need. When off, an order up to one and a half times your quantity is allowed with a warning.
- **Required certifications:** certifications every vendor should have, in addition to any listed in the product's own specs.
- **Strict certifications:** reject vendors missing a required certification. When off, they only get a lower score and a warning.
- **Strict minimum price:** reject vendors priced below the product's minimum price. When off, they only lose a few points and get a warning.

## Weights

Weights decide how much each factor counts. They must be zero or higher. Each group is scaled to add up to 100, and the scaled values are returned in the response.

**Vendor score weights**
Price, minimum order quantity, trade assurance, years active and certifications. They make up the vendor score. At least one must be above zero.

**Blend weights**
Vendor score against RFQ fit. RFQ fit measures how well the listing matches the product's specs and brand. At least one must be above zero.

## Tiers

The final score is out of 100.
- 75 and above: Strong Recommend.
- 55 up to 75: Consider.
- Below 55: Caution.

## Built-in warnings

- A price far below the group's median is flagged as suspicious and not rewarded. This needs at least three priced vendors.
- A unit that differs from the one you asked for is flagged.
- Brand wording such as "or similar" allows alternatives with a warning. Without it, a different brand scores low.

## Save results

When this is on, the scoring result and Excel file are saved for your account. When off, you get the result back but nothing is stored.
