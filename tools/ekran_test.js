// Karıştırılmış index.html'i gerçek tarayıcıda açar; hata varsa ya da liste boşsa 1 döner (yayın yapılmaz).
const { chromium } = require("playwright");
(async () => {
  const b = await chromium.launch();
  const c = await b.newContext({ userAgent: "Mozilla/5.0 (Linux; Android 14) AppleWebKit/537.36 Chrome/126 Mobile Safari/537.36 HisseRadarApp/12", viewport: { width: 390, height: 844 } });
  const p = await c.newPage();
  const hatalar = [];
  p.on("pageerror", e => hatalar.push(e.message));
  await p.addInitScript(() => { localStorage.setItem("onay", JSON.stringify("x")); localStorage.setItem("iosIpucu", "9"); });
  await p.goto("http://localhost:8765/index.html");
  await p.waitForTimeout(6000);
  const onay = await p.$("#onay1");
  if (onay) { await p.check("#onay1"); await p.check("#onay2"); await p.click("#onayBtn"); await p.waitForTimeout(500); }
  const satir = await p.$$eval("#p-piyasa [data-k]", e => e.length);
  for (const t of ["beklenti", "haber", "fon", "portfoy", "piyasa"]) { await p.click(`nav button[data-tab="${t}"]`); await p.waitForTimeout(700); }
  console.log("satır:", satir, "hatalar:", hatalar, "onay ekranı:", !!onay);
  await b.close();
  process.exit(hatalar.length || satir < 20 ? 1 : 0);
})().catch(e => { console.error(e); process.exit(1); });
