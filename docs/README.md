# docs/

Source of the docs site (GitHub Pages, "Deploy from a branch", folder `/docs`). Plain Markdown, no build step.

| path | what | how it is made |
|---|---|---|
| `index.md` | site home | by hand |
| `rack-parts/index.md`, `rack-parts/img/*.png` | rack-parts catalog | generated: `cd rack-parts && python3 -m rackparts catalog --out build/catalog`, then copy `build/catalog/` here. CI regenerates it and fails if the committed copy differs. |
