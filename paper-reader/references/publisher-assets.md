# Publisher PDF and Figure Asset Rules

This file records the publisher URL patterns used by `paper-reader/publisher_rules.py`.
It is meant for maintenance after F12 inspection. Do not put these rules in
`_shared/user-config.json`: they are versioned parser behavior, not user
preferences.

## General F12 Procedure

1. Open the article landing page with the same login/cookies used by Codex.
2. Inspect the first real article figure, not the graphical abstract.
3. Prefer direct downloadable image files over screenshots.
4. Check `<figure>`, `meta[name^="citation_"]`, `srcset`, lazy-load attributes,
   "open in new tab" links, and Network image requests.
5. Record stable path patterns. Avoid storing expiring signed query strings as
   canonical rules.
6. If a direct HTTP request gets 403 but the browser can read the asset, use
   the Playwright/Patchright context request with credentials.

## Known Patterns

### Wiley

- Article host: `onlinelibrary.wiley.com`
- Fig1 pattern seen in production:
  `https://onlinelibrary.wiley.com/cms/asset/<uuid>/<article>-fig-0001-m.jpg`
- High-resolution candidate is usually the same path without the size suffix:
  `<article>-fig-0001.jpg`
- PDF direct route:
  `https://onlinelibrary.wiley.com/doi/pdfdirect/<DOI>?download=true`
- Plain HTTP can hit Cloudflare even when the browser profile is authorized.
  Treat browser-context success as the canonical path.

### OUP / Silverchair / Molecular Biology and Evolution

- Article host: `academic.oup.com`
- Large-view link can look like:
  `/view-large/figure/<id>/<file>.jpg`
- Final image can be an expiring signed Silverchair CDN URL.
- Thumbnail CDN paths often contain `/m_<file>.jpeg`; the full image is often
  the same signed URL path without `m_`.
- Store the path convention, not the full signed URL.

### Cell / Elsevier

- Cell Fig1 pattern:
  `/cms/<doi>/asset/<uuid>/main.assets/gr1.jpg`
- Cell high-resolution Fig1 may use:
  `/cms/<doi>/asset/<uuid>/main.assets/gr1_lrg.jpg`
- Cell graphical abstract pattern:
  `/cms/<doi>/asset/<uuid>/main.assets/ga1.jpg`
- ScienceDirect Fig1 pattern seen in production:
  `https://ars.els-cdn.com/content/image/1-s2.0-<compactPII>-gr1.jpg`
- Prefer `gr1.jpg` / `gr1_lrg.jpg` for Figure 1. Reject `ga1.jpg` when the task
  asks for the first article figure rather than graphical abstract.
- For Elsevier/ScienceDirect full text, use the Elsevier API first when
  `sources.elsevier_api_key` is configured. The API provides text/XML and PII;
  PDF still depends on browser access to the publisher page.
- For `10.1016/*` DOI inputs, resolve the API PII directly to `cell.com/<journal>/fulltext/<PII>`
  for known Cell Press journals, or `sciencedirect.com/science/article/pii/<compactPII>`
  for other Elsevier journals. This avoids the more fragile `doi.org` / LinkingHub path.
- ScienceDirect non-open PDFs may return HTML with HTTP 200; treat that as
  subscription/paywall, while still keeping API text and HTML Fig1 if available.

### Science / PNAS

- The article DOM usually exposes real image URLs.
- Direct unauthenticated HTTP can return 403.
- Download through page `fetch(..., credentials: "include")` or context request.

### Nature / Springer / BMC

- `media.springernature.com` image assets are usually directly downloadable.
- Springer PDF routes:
  - `10.1186/*`: `https://link.springer.com/content/pdf/<DOI>_reference.pdf`
  - `10.1007/*`: `https://link.springer.com/content/pdf/<DOI>.pdf`

## Fallback Policy

- HTML image success means a real `image/*` file was downloaded.
- Screenshots do not count as successful Figure 1 extraction.
- If publisher HTML assets are unavailable, fall back to PDF Figure 1 extraction.
- If both HTML and PDF assets are unavailable, continue note generation with
  text/abstract evidence instead of blocking the whole note.
