# Browser control

The agent browses with one browser, and you watch it **in the chat**. Ask it to
open a site, click through a flow, fill a form, or take a screenshot of what it
found: the turn shows a live view of the page while it works, and collapses to a
one-line record (a thumbnail, the step count, Replay) when it is done.

Click the live view at any point to **take over** with your real mouse and
keyboard; the same card grows and says "You are in control". Press Esc, or
"Hand back", to give it back. That is how a sign-in, a CAPTCHA or a two-factor
prompt gets handled: the agent stops, you do the step, it carries on. While you
hold the browser, the agent waits.

## Which browser

| Where Kiro Crew runs | Browser the agent drives | First run |
|---|---|---|
| A desktop with Chrome | your own Chrome, with your logins | enable **Allow remote debugging** at `chrome://inspect/#remote-debugging` once, then click **Allow** when Chrome asks |
| Windows without Chrome | Edge | same |
| A server, no display, or a remote gateway | Kiro's own Chromium, headless | none |
| Chrome's remote-debugging policy is off | Kiro's own Chromium | none |

The agent acts only on tabs it opened; it never touches the ones you have open.
Kiro's own Chromium keeps its own profile (so a login you did during a takeover
stays signed in next time), and the agent cannot read that profile's files.

## What the agent can do to a page

Navigate to a URL, go back, read the page as a structured outline of its
elements, click (by element or by position, single or double), drag, scroll,
type, press a key, hover, choose from a dropdown, wait for text or for the page
to load, read the browser console, take a screenshot (saved as a PNG it can show
you in the chat), and list or switch its own tabs.

It **never types a password**. A password field is refused, and the live view
masks one, so a sign-in is always your step through takeover.

Reading a page it can already reach is cheaper than driving it, so a request that
only needs the text of a public page may be answered without the browser at all.
Driving it is for the cases that need interaction, a logged-in session, or a page
whose content only exists once its scripts have run.

## Local addresses are refused

The agent cannot navigate to `localhost`, a loopback address, or a literal
private-network or link-local IP address. Those are where your own control
planes live, this dashboard among them, so driving one would let the agent reach
an interface it is not supposed to operate. Only public HTTP(S) targets open,
plus the exact `host:port` origins you list under **Settings → Browser → Local
preview origins** (a dev server you want it to check, say). The dashboard's own
port is refused even when listed.

The gate does not resolve DNS names. A hostname that later resolves to a private
address can therefore pass this check, so a successful navigation is not proof
that the destination is public.

## The live view

Frames stream only to your own dashboard session, over the connection it already
has, at up to 60 frames per second on a fast link. The stream adapts to the
link: on a slow one it drops the frame rate and quality first, then the size,
and climbs back when the link recovers. A page that is not changing costs
nothing, and so does a card that has scrolled off screen. Nothing is written to
disk unless the agent takes a screenshot you asked for.

**Settings → Browser → Live view in chat** turns the view off, caps the frame
rate, picks a quality, and controls the password mask (on by default).

## Preview tab

The side panel keeps one browser surface of its own: the **Preview** tab, for a
local dev server the agent started for you. It opens only the origins listed
under **Local preview origins**, and the agent does not drive it; an outside URL
there answers "Open this in your Chrome, or ask Kiro".

## Settings → Browser

| Section | What it holds |
|---|---|
| Browser in use | which browser is connected, its state, the fallback order, Test, Disconnect |
| Engine | the browser engine version; Kiro's Chromium version, Redownload |
| Privacy | no usage data leaves the machine and no cloud browser is used (not configurable) |
| Live view in chat | show the live view, quality, maximum frame rate, mask password fields |
| Local preview | the Preview tab's origins |
| Advanced | pick a browser by hand: automatic, your Chrome, Kiro's Chromium, headless |

The first visit walks through three steps (install the engine, connect your
Chrome, try it); **Skip** uses Kiro's own Chromium instead.

## Related docs

- [Dashboard](dashboard.md): the side panel and where its tabs live
- [Computer use](computer-use.md): driving native desktop apps rather than a web page
- [Artifacts](artifacts.md): keeping a screenshot or a page the agent produced
