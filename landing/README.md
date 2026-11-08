# Retano landing

The public landing page for ریتانو, served at `https://retano360.com/`. It is a plain static site
(HTML, CSS and JavaScript, no build step) and is separate from the panel in `webApp/`.

```
landing/
├── index.html              all page content (Persian, right to left)
└── assets/
    ├── css/landing.css     styles, mobile first
    ├── js/config.js        panel and API addresses  ← edit here
    ├── js/stages.js        the small animated scenes (customers drawn as little people)
    ├── js/landing.js       calculators, sample SMS, before/after, consultation form
    ├── fonts/              IRANYekanX (woff2)
    └── img/                favicon and icons
```

## Run it locally

Any static file server works. From the repository root:

```
cd landing
python3 -m http.server 5500
```

Then open http://localhost:5500. Opening `index.html` directly from disk also works.

When the page runs on `localhost` (or from disk), the «ورود به پنل» buttons point to the panel's
dev server at `http://localhost:8080/#/login`. Start the panel with
`./gradlew :webApp:wasmJsBrowserDevelopmentRun` to try the whole flow.

## Addresses

`assets/js/config.js` holds the two addresses the page uses:

- `panelUrl`: where every «ورود به پنل» button goes. Default `https://panel.retano360.com/#/login`.
- `apiBase`: the Retano API used by the free consultation form. Default `https://panel.retano.ir/api/v1`.

## Deploy

Upload the contents of `landing/` to the web root of `retano360.com`. Nothing needs to be compiled.

The consultation form sends `POST {apiBase}/free-consults/` as a simple form post
(`application/x-www-form-urlencoded`, field `phone_number`), so browsers send it without a CORS
preflight. The API still has to allow the landing's origin (`https://retano360.com`) in its
`Access-Control-Allow-Origin` response header, or the browser will hide the response and the form
shows a connection error.

## Look and animation

The page uses the panel's light palette (the same teal, greys and brand mark as `RetanoColors.kt`),
so moving from the landing to the panel feels like one product.

Text always sits on a plain background. The animation lives only in a few framed "stages" next to
the text, each an element with `data-stage` holding a `<canvas>` that `stages.js` draws on. Every
little person is one customer, and the colours mean the same thing everywhere: teal bought again,
amber at risk, grey stopped buying, rose lost.

- `attract` (hero): a message travels from the store to a customer who stopped buying; they light up
  and walk back, and their purchase rises from the store while the "today" counter adds it up. A
  card tells the story of one customer, Sara, step by step; pointing at any other customer shows
  theirs instead.
- `hundred` (what Retano does): out of 100 first-time buyers, 65 turn grey and drift away; then a
  frame is drawn around them: "Retano finds these 65".
- `audience` (campaign calculator): 200 SMS recipients; the number who buy follows the conversion
  rate slider, and the first buyers show what they spent.
- `crowd` (a solution for every customer): the customers sort themselves into three groups; the
  group of the chosen tab lights up and its message travels along a dashed line to the phone.
  Sara is in the lost group, and the sample message for that group is hers.
- `loop` (before/after): customers who drifted away rejoin the circle as the slider moves to "after".
- `falling` (lost sales): the share of customers set by the slider quietly drops away, and a counter
  adds up this month's lost customers and sales.
- `retained` (consultation form): loyal customers circling the store, Sara among them; it lights up
  after a request is sent.

Only stages on screen are animated, and `landing.js` passes the slider values in through
`window.RetanoScene`. Visitors who ask their system for reduced motion get still pictures.
