# Templates

Each folder here is one resume design. Pick one with `template:` under `settings` in `data/experiences.yaml`. The default is `default`.

A template folder holds:
- `main.tex.j2`: the resume's LaTeX, with placeholders where your content goes
- anything else the design needs: `.cls`, `.sty`, fonts, images (copied in when it compiles)
- `template.json` (optional): `{"main": "main.tex.j2", "overleaf_engine": "pdflatex"}`. Use `xelatex` if the design uses custom fonts (`fontspec`).

## Adding an Overleaf template

1. Open the template in Overleaf, then go to Menu, then Download, then Source. You get a `.zip`.
2. Unzip it into a new folder here, for example `templates/jakes/`.
3. Rename the main `.tex` file to `main.tex.j2` and swap the hardcoded content for placeholders:

```
\VAR{p.first_name} \VAR{p.last_name}   name     (also p.email, p.phone, p.location)
\BLOCK{for l in p.links} ... \VAR{l.label} / \VAR{l.url_raw} ... \BLOCK{endfor}

\BLOCK{for x in experiences}
  \VAR{x.title}  \VAR{x.org}  \VAR{x.location}  \VAR{x.dates}
  \BLOCK{for b in x.bullets} \item \VAR{b} \BLOCK{endfor}
\BLOCK{endfor}

\BLOCK{for x in projects}  \VAR{x.title} \VAR{x.stack} \VAR{x.dates}  (bullets as above)  \BLOCK{endfor}
\BLOCK{for e in education} \VAR{e.school} \VAR{e.degree} \VAR{e.location} \VAR{e.dates} \VAR{e.details} \BLOCK{endfor}
\BLOCK{for s in skills}    \VAR{s.category}: \VAR{s.list} \BLOCK{endfor}
```

4. Set `template: jakes` in your bank's `settings`.

`default/main.tex.j2` is a full working example. Or put the Overleaf `.zip` in the project folder and ask Claude to convert it.

Everything is LaTeX-escaped for you, so bullets with `&`, `%` or `#` are fine.
