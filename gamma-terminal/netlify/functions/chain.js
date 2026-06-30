exports.handler = async function (event) {
  const CORS = {
    "Access-Control-Allow-Origin": "*",
    "Content-Type": "application/json",
  };

  if (event.httpMethod === "OPTIONS") {
    return { statusCode: 204, headers: CORS, body: "" };
  }

  const qs = event.queryStringParameters || {};
  const symbol = (qs.symbol || "SPX").toUpperCase();
  const INDEX_SYMBOLS = ["SPX", "VIX", "NDX", "RUT", "XSP", "VIX3M", "VVIX"];
  const cboeSymbol = INDEX_SYMBOLS.includes(symbol) ? "_" + symbol : symbol;
  const url = `https://cdn.cboe.com/api/global/delayed_quotes/options/${cboeSymbol}.json`;

  try {
    const res = await fetch(url, {
      headers: {
        "User-Agent": "Mozilla/5.0 (GammaTerminal/1.0)",
        Accept: "application/json",
      },
    });
    if (!res.ok) {
      return {
        statusCode: res.status,
        headers: CORS,
        body: JSON.stringify({ error: "CBOE returned HTTP " + res.status }),
      };
    }
    const data = await res.json();
    return {
      statusCode: 200,
      headers: { ...CORS, "Cache-Control": "public, max-age=60" },
      body: JSON.stringify(data),
    };
  } catch (e) {
    return {
      statusCode: 502,
      headers: CORS,
      body: JSON.stringify({ error: e.message }),
    };
  }
};
