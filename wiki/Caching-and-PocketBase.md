# Caching and PocketBase

## Where search results are saved

Search results are not stored in PocketBase. They are saved in one JSON file on the backend, in the output folder, named `family_scrape_cache.json`. In Docker this lives on a persistent volume, so it survives restarts.

## How a saved result is found

Each family search has a key. The key is the search text, trimmed and made lowercase. If two families, runs or users produce the same search text, they share one saved result. Changing positions, limit or the Excel file does not matter, as long as the search text is the same.

## What is saved

For each search: the family name, the original search text, and the list of vendor listings. It also records when it was saved, how many pages were read, whether stealth was on, and how many credits it cost.

## What is reused and what is ignored

A saved result is reused whenever it exists and holds a list of listings. Pages, stealth and max products per page are recorded but not compared. So a result saved with one page is reused when you ask for three pages. To get more pages, evict that entry or use force refresh.

## When a result is saved

A family is saved right after it finishes, so an interrupted or cancelled job keeps its finished work. It is saved only if every page worked and at least one listing came back. A family with a failed page, or with zero listings, is not saved. It is searched again on the next run. Credits spent on such a family are still counted in the job total.

## Sharing and safety

The cache is shared by all users. This is intended: the same search gives the same listings, so sharing saves credits. Only admins can remove an entry, because refilling it costs credits. Removing one entry only affects that one search.

Writes are safe against crashes and simultaneous requests. The file is written to a temporary copy and then swapped in, under a lock.

Older saved entries that use the earlier raw format are converted when read.

## What PocketBase stores

PocketBase handles sign-up and login, and keeps a copy of some records for the frontend to show. Each record belongs to one user, and users can only see their own.

- **Users:** accounts, passwords and email verification.
- **RFQ files:** the file name, row count and sheet names of each upload.
- **Scrape jobs:** each job's status, family progress, credits spent and any error.
- **Runs:** a summary and dashboard numbers from the latest scoring run for that user.

These records are only for display. The backend files are the real source for credits and job state. If PocketBase is down, the backend keeps working and skips the copy.

## Where everything else lives

- Uploaded Excel files, job records and each user's results are files on the backend.
- Raw responses that needed repair are saved in the debug folder. Only admins can list them.
