# Upload page

The React/TypeScript frontend for the local upload page (`ia_bulk.py serve`,
issue #29). It has no logic of its own — every decision it shows comes from a
real `validate`/`upload` run on the other side of the API; see
[`../docs/ARCHITECTURE.md`, "`serve` and the upload page"](../docs/ARCHITECTURE.md#serve-and-the-upload-page)
for what the server does, and
[`../docs/decisions/UPLOAD-PAGE.md`](../docs/decisions/UPLOAD-PAGE.md) for why
the page is built this way.

Built with Vite, React, TypeScript, Tailwind v4, Radix and zod; tested with
Vitest and Testing Library.

## Dev loop

The Mac this page runs on in production has no Node — it serves the
committed `dist/` bundle directly (see "Building and committing `dist/`"
below). Node is only needed here, on a development machine.

1. `yarn install` once (or again after `package.json`/`yarn.lock` change).
2. In one terminal, from the repo root, run the real server against the test
   Sheet — never `--live` for frontend work:

   ```bash
   python ia_bulk.py serve --registry e2e_fixtures/registry.json --project e2e
   ```

3. In another terminal, from `upload_page/`, run `yarn dev`. Vite's dev
   server proxies `/api/*` and `/assets/*` to `http://127.0.0.1:5277`
   (`vite.config.ts`), rewriting the proxied request's `Origin` to the
   upload server's own so its same-origin request guard never needs a
   dev-only exception — see
   [`../docs/decisions/UPLOAD-PAGE.md`, "The server's request guard"](../docs/decisions/UPLOAD-PAGE.md#the-servers-request-guard).
   Open the URL Vite prints; the page talks to the server in step 2 through
   that proxy.

## Building and committing `dist/`

The Mac never runs `yarn build` — it serves the `dist/` this repo already
carries. So after any change under `src/` (or to `index.html`,
`package.json`, `yarn.lock`, `tsconfig.json`, `vite.config.ts` or
`vitest.config.ts`), rebuild and commit the result:

```bash
yarn build
```

This runs `vite build`, then `scripts/build-stamp.mjs`, which writes
`dist/build-stamp.json` — a content hash over exactly those build inputs.
`../build_stamp.py` computes the same hash on the Python side
(`STAMP_ALGORITHM` in both files must agree byte for byte), so the server can
tell the bundle's stamp without needing Node installed to rebuild and check.

Commit `dist/` (including `dist/build-stamp.json`) along with the source
change that caused it. Forgetting to rebuild, or rebuilding but not
committing, is caught on the very next `python -m pytest`: the guard test
`test_committed_bundle_is_current` in `../test_build_stamp.py` recomputes the
stamp from the checked-in source and fails the whole suite if it no longer
matches `dist/build-stamp.json` — so a stale bundle never reaches the Mac
silently. `upload_server.py` itself only checks at startup that a stamp file
exists at all, refusing to serve (printing why, exiting `0`) if the page was
never built — see
[`../docs/decisions/UPLOAD-PAGE.md`, "KeepAlive restarts only on failure"](../docs/decisions/UPLOAD-PAGE.md#keepalive-restarts-only-on-failure).

## Design tokens

Every color, radius and font lives in `src/index.css`: CSS custom properties
on `:root` for light mode, redefined inside a `prefers-color-scheme: dark`
block for dark mode, then re-exposed to Tailwind's utilities through a
`@theme inline` block. Change a token in that one pair of places — never a
raw color value inside a component — so light and dark stay in sync.

## Tests and type-checking

```bash
yarn typecheck
yarn test
```

`yarn test` runs Vitest once (`vitest run`, not watch mode). `python -m
pytest` on the repo root never descends into this directory — `upload_page`
is in `pytest.ini`'s `norecursedirs`, alongside `node_modules` underneath it.
