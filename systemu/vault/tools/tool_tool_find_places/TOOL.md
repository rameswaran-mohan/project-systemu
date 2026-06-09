---
name: find_places
tool_type: web
status: deployed
enabled: true
dependencies:
  []
---

# find_places

## Description

Structured local business / point-of-interest lookup near a location (OpenStreetMap). Use for 'X shops/gyms/restaurants near me' — returns names, addresses, hours. NOT for general web search.

## Parameters

- query (string): What to look for, e.g. 'gym', 'pharmacy', 'coffee shop'
- near (string, optional): Place name to center the search on (geocoded via OSM), e.g. 'Chennai'. Optional if lat/lon are supplied.
- lat (number, optional): Latitude of the search center. Optional alternative to `near`.
- lon (number, optional): Longitude of the search center. Optional alternative to `near`.
- limit (integer, default: 10): Maximum number of places to return

## Returns

- success (boolean)
- places (array) — List of {name, opening_hours, address, phone, lat, lon}
- attribution (string) — Required ODbL attribution for OpenStreetMap data
- center (object) — Resolved {lat, lon} the search was centered on
- error (string)

## Implementation Notes

Routes to systemu.runtime.web_access.find_places — keyless OSM Nominatim geocode + Overpass POI lookup, ODbL-attributed. Returns named local businesses only (unnamed nodes are dropped). Always surface the `attribution` field when displaying results.
