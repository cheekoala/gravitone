# Credits

Gravitone ships no third-party code — the program is Python's standard
library and the page is hand-written HTML, CSS and JavaScript. Two things in
it were made by other people, and both are free to use.

## Icons — Lucide

The icons in the control panel are [Lucide](https://lucide.dev), used under
the **ISC licence**. The paths are inlined into `gravitone/ui/index.html` and
`app.js` rather than loaded from a CDN, because the page must work with no
network at all.

```
ISC License

Copyright (c) for portions of Lucide are held by Cole Bemis 2013-2022 as
part of Feather (MIT). All other copyright (c) for Lucide are held by
Lucide Contributors 2022.

Permission to use, copy, modify, and/or distribute this software for any
purpose with or without fee is hereby granted, provided that the above
copyright notice and this permission notice appear in all copies.

THE SOFTWARE IS PROVIDED "AS IS" AND THE AUTHOR DISCLAIMS ALL WARRANTIES
WITH REGARD TO THIS SOFTWARE INCLUDING ALL IMPLIED WARRANTIES OF
MERCHANTABILITY AND FITNESS. IN NO EVENT SHALL THE AUTHOR BE LIABLE FOR ANY
SPECIAL, DIRECT, INDIRECT, OR CONSEQUENTIAL DAMAGES OR ANY DAMAGES
WHATSOEVER RESULTING FROM LOSS OF USE, DATA OR PROFITS, WHETHER IN AN
ACTION OF CONTRACT, NEGLIGENCE OR OTHER TORTIOUS ACTION, ARISING OUT OF OR
IN CONNECTION WITH THE USE OR PERFORMANCE OF THIS SOFTWARE.
```

## The Gravitone mark

`docs/icon.svg` — a mass with the waves coming off it — was drawn for this
project and is under the same **MIT licence** as the rest of it. Four ellipse
arcs and a circle; no font, no raster, no tracing of anyone else's work.

## Everything else

The player shells out to whatever is installed — **ffmpeg** (ffplay,
ffprobe), **mpv**, **VLC**, **afplay** — and talks to **PulseAudio** or
**PipeWire** through `pactl` for live volume. None of them are bundled or
linked; Gravitone runs whichever it finds and does without when it finds
none.
