from urllib.parse import urlparse, urlunparse, parse_qsl, urlencode

def normalize_url(url: str) -> str:
    if not url:
        return ""
    
    parsed = urlparse(url.strip())
    # Normalize scheme and host to lowercase and omit default ports.
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    if (scheme == "https" and netloc.endswith(":443")) or (scheme == "http" and netloc.endswith(":80")):
        netloc = netloc.rsplit(":", 1)[0]
    
    # Strip common tracking query parameters
    tracking_params = {
        "utm_source", "utm_medium", "utm_campaign", "utm_term", "utm_content",
        "fbclid", "gclid", "yclid", "_hsenc", "_hsmi", "mc_cid", "mc_eid"
    }
    
    q_params = parse_qsl(parsed.query)
    filtered_q_params = [(k, v) for k, v in q_params if k.lower() not in tracking_params]
    
    # Sort query parameters to guarantee URL equality uniqueness
    filtered_q_params.sort()
    normalized_query = urlencode(filtered_q_params)
    
    # Equivalent trailing-slash forms are treated as one URL, except for root.
    path = parsed.path or "/"
    if path != "/":
        path = path.rstrip("/")

    # Strip fragment identifiers.
    return urlunparse((scheme, netloc, path, parsed.params, normalized_query, ""))
