// Uygulama ekranının JavaScript kodunu okunamaz hâle getirir (tools/build_index.py çağırır).
// Kullanım: node tools/karistir.js <girdi.js> <cikti.js>
const fs = require("fs");
const JO = require("javascript-obfuscator");
const [, , girdi, cikti] = process.argv;
const kod = fs.readFileSync(girdi, "utf8");
const sonuc = JO.obfuscate(kod, {
  compact: true,
  target: "browser",
  identifierNamesGenerator: "hexadecimal",
  renameGlobals: false,          // Android'in çağırdığı window.hrBack gibi adlar korunmalı
  stringArray: true,
  stringArrayEncoding: ["base64"],
  stringArrayThreshold: 0.75,
  rotateStringArray: true,
  shuffleStringArray: true,
  splitStrings: true,
  splitStringsChunkLength: 8,
  numbersToExpressions: true,
  simplify: true,
  controlFlowFlattening: false,  // telefonda yavaşlatmasın
  deadCodeInjection: false,
  selfDefending: false,
  unicodeEscapeSequence: false,
  seed: 0
});
fs.writeFileSync(cikti, sonuc.getObfuscatedCode());
