# Third-party notices

## TradingView Lightweight Charts v4.2.3

- File: `scalplab/webui/static/lightweight-charts.standalone.production.js` (unmodified standalone production build, vendored so the Web UI works offline)
- Source: https://unpkg.com/lightweight-charts@4.2.3/dist/lightweight-charts.standalone.production.js
- Copyright (c) 2025 TradingView, Inc.
- License: Apache License 2.0, https://www.apache.org/licenses/LICENSE-2.0 (the original `@license` banner is kept at the top of the file)
- Attribution: the library's default `attributionLogo` is left enabled, so each chart links to https://www.tradingview.com/. `app.js` does not turn it off.

The rest of the repository is covered by the MIT license in `LICENSE`.

## Test fixtures

`tests/fixtures/matsui_tick_page1.html` and `matsui_tick_page2.html` are truncated excerpts (a handful of rows each) of the markup of a public ranking page, kept only to test the HTML parser in `scalplab/datafeed/jp_tick_ranking.py`. They are not part of the licensed work.
