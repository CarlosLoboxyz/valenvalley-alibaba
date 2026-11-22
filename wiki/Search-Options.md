# Search Options

These settings control the grouping step and the live Alibaba search. Grouping is free. Only a live search job costs credits.

## Which products to include

**RFQ file**
The Excel file to use. You can pick the built-in sample or one you uploaded. You can only use your own uploads.

**Limit**
Only use the first N products in the file. Fewer products means fewer families and a smaller bill.

**Positions**
Only use specific rows by their position number. You can give single numbers, ranges, or both. If you also set a limit, positions are applied first, then the limit.

## How products are grouped

**Granularity**
How aggressively similar products are merged into one family. Each family becomes one Alibaba search.
- Fine keeps families small and specific. You get more searches and more credits spent.
- Balanced is the default middle ground.
- Broad merges the most. You get the fewest searches and the lowest cost, but a family may hold products that are only loosely alike.

The grouping runs on your own server with no AI service involved. The same input always gives the same families.

## How each search runs

**Pages**
How many pages of Alibaba results to read for each family. Each page costs credits, so cost grows directly with this number. More pages gives more vendors to compare.

**Stealth**
Chooses the proxy type used to reach Alibaba.
- Off uses the premium proxy and costs 30 credits per page.
- On uses the stealth proxy and costs 80 credits per page. Use it only if the normal mode keeps getting blocked.

**Max products per page**
The most listings to read from each results page. Ads are skipped. A lower number reads less of each page.

**Force refresh**
Ignore saved results and search again, even for families that are already saved. This spends credits on every selected family. Leave it off unless you believe saved data is out of date.

**Family queries**
Limit the job to a chosen set of searches from the current plan. Everything else is read from saved results only and costs nothing. A search that is not in the current plan is rejected.

**Confirm credit spend**
A required yes. Without it a live search job is refused. Nothing in the app spends credits without this, and uploading, grouping and scoring never do.

## What a job costs

The estimate is the number of families that need searching, times pages, times the per-page rate (30 or 80). Families already saved are not counted unless force refresh is on.

The real cost is read from ScrapingBee's own report after each page and added up, so the final number can differ from the estimate.

## Safety limits set by the server owner

**Max credits per job**
A job whose estimate is above this number is refused before it starts.

**Max credits per day**
A job is refused if it would push total spending for the day past this number. This is shared by all users.

**One job at a time**
Each user can run one live job at once. Two jobs also cannot search the same query at the same time.

**Verified email**
Users must verify their email before they can start a live job, unless the server owner turned this off.

## Cancelling

Cancel stops the job before the next page or family. A page already being fetched finishes first. Everything already saved stays saved.

## Settings only available on the command line

**Query suffix**
Words added to the end of every family search. The web app uses the default.

**Stage 2 similarity**
A manual number that overrides granularity for the second merging step.
