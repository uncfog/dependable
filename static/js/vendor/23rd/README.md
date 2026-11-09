# 23rd components, as plain JavaScript

The landing page's five effects come from the 23rd registry
(https://github.com/radiumcoders/23rd.dev, commit e59481f, 1 November 2026):
`shader-gradient`, `typewriter`, `iso-lattice`, `live-orb` and `stretchy-footer`.
Each is that registry's own framework-free engine (`registry/<name>/<name>-vanilla.ts`),
compiled to an ES module with esbuild 0.25.10 and nothing else changed:

    npx esbuild@0.25.10 registry/<name>/<name>-vanilla.ts --format=esm --target=es2020         --minify-syntax --legal-comments=inline --outfile=<name>-vanilla.js

`static/js/landing.js` starts them. To update one, rebuild it from a newer commit
the same way and note the commit here.

The registry is published for projects to install and own (its shadcn registry and
agent skill both say so) and is funded by sponsorship, but at that commit the
repository carries no LICENSE file. Its owner was told this when the page shipped.
