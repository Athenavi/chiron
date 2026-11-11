# Typst Font Assets

This directory vendors the default `text` font assets used by `@myriaddreamin/typst.ts`.

Source: `typst-assets@v0.13.1`, `files/fonts/`

The 17 font files are unmodified copies from
https://github.com/typst/typst-assets/tree/ad8080d46d42fca909562572cfa14a86f00eb945/files/fonts.
The complete upstream attribution and font grants are retained in
`NOTICE.typst-assets.txt`, together with `LICENSE.typst-assets.txt` and the
complete `LICENSE.LPPL-1.3c.txt` referenced by the NewComputerModern GUST grant.
Libertinus uses SIL OFL 1.1; NewComputerModern uses the GUST grant incorporating
LPPL 1.3c or later; DejaVu retains its Bitstream and Arev notices. These font
terms remain applicable when the fonts are copied into viewer asset packages.

The files are copied into `wasm/typst/fonts/` by File Viewer asset tooling so Typst previews do not fetch fonts from public CDNs at runtime. Keep the filenames stable because `typst.ts` resolves them by name.
