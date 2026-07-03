from . import antonsport, fjellsport, intersport, sport1, sportoutlet, xxl

# Each module exports get_sku_count(page) for the whole catalog, and may
# optionally export either get_category_counts(page) -> {name: count}
# (direct, e.g. API-based) or get_categories(page) -> {name: url} plus a
# get_sku_count(page, start_url) overload for per-category counts.
SITES = [
    ("sportoutlet", sportoutlet),
    ("xxl", xxl),
    ("antonsport", antonsport),
    ("intersport", intersport),
    ("sport1", sport1),
    ("fjellsport", fjellsport),
]
