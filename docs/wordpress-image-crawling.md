# WordPress Image Crawling Plan

Most customer sites are WordPress sites, so product image collection should prefer WordPress-native data before falling back to page scraping.

## Preferred Order

1. REST API discovery
   - Try `https://domain/wp-json/`.
   - If blocked, try `https://domain/?rest_route=/`.
   - Confirm available routes from the API index where possible.

2. Media library search
   - Endpoint: `/wp-json/wp/v2/media`.
   - Use query parameters:
     - `media_type=image`
     - `search=<product name or keyword>`
     - `per_page=20`
   - Read:
     - `source_url`
     - `alt_text`
     - `caption.rendered`
     - `media_details.sizes`
   - Prefer the largest useful WebP/JPG/PNG size under the same domain.

3. Page and post search
   - Endpoints:
     - `/wp-json/wp/v2/pages?search=<product>&_embed&per_page=10`
     - `/wp-json/wp/v2/posts?search=<product>&_embed&per_page=10`
   - Read `featured_media`.
   - If `_embedded.wp:featuredmedia` exists, use its `source_url`.
   - Otherwise request `/wp-json/wp/v2/media/<featured_media>`.

4. Product URL page scrape fallback
   - Fetch the product page HTML.
   - Extract images from:
     - `meta[property="og:image"]`
     - `meta[name="twitter:image"]`
     - WooCommerce gallery selectors such as `.woocommerce-product-gallery img`
     - Generic `img` tags inside `main`, `article`, or product sections
   - Normalize `src`, `data-src`, `srcset`, and lazy-load attributes.
   - Reject logos, icons, placeholders, tracking pixels, and very small images.

5. Download and save
   - Save into task directory: `images/`.
   - Hero image name: sanitized article title.
   - Product images: sanitized product name.
   - Store metadata in `product_assets/images.json`.

## Ranking Rules

Score candidate images by:

- URL or alt text contains product name or keyword.
- Source page is the confirmed product URL.
- Image dimensions are large enough for Word insertion.
- File is not a logo, icon, sprite, avatar, placeholder, or banner-only asset.
- Same-domain images rank above CDN images unless CDN path clearly belongs to the site.

## Notes

- WordPress media, posts, and pages are usually public, but some sites disable REST access or block requests. The fallback HTML method is required.
- `_embed` reduces extra API calls by embedding linked resources when WordPress marks them embeddable.
- If a customer provides product URLs manually, crawl those URLs first and use site-wide media search only as a fallback.

## Official References

- WordPress REST API Reference: https://developer.wordpress.org/rest-api/reference/
- Media endpoint: https://developer.wordpress.org/rest-api/reference/media/
- Posts endpoint: https://developer.wordpress.org/rest-api/reference/posts/
- Pages endpoint: https://developer.wordpress.org/rest-api/reference/pages/
- Linking and embedding: https://developer.wordpress.org/rest-api/using-the-rest-api/linking-and-embedding/
