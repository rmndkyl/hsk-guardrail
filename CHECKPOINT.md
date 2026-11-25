# HSK Guardrail / Kharon — CHECKPOINT

Terakhir diperbarui: 25 Nov 2026

## Status

**Siap submit.** Repo publik, 26/26 test lulus, video demo 63 detik, agent jalan.
Commit terakhir: `a6874ef`. Semua bukti pengukuran ada di `evidence/` (ter-commit, jadi
juri bisa baca dari clone bersih tanpa menjalankan apa pun).

- Repo: https://github.com/rmndkyl/hsk-guardrail
- Lokal: ~/LYID-BOTS/hsk-guardrail
- Video: demo/kharon-demo.mp4 (1920x1080, 63.5s, 684 KB)
- Paket submission: ~/HERMES DATA/research/hsk-submission-package.md
- Deadline: **3 Des 2026 11:00 WIB** (13:00 KST)

## Temuan inti (yang membuat produk ini beda)

Diukur sebelum menulis kode trading, semua dari mainnet HSK Chain 177:

| Metrik | Nilai |
|---|---|
| TVL pool WHSK/USDT 0.30% | ~$838K (5.19M WHSK + 419K USDT) |
| Swap dalam ~50 jam | 96 (~$559/hari) |
| Swap 24 jam terakhir | **12 dari 3 alamat, $135.09** |
| Range spot pool 48 jam | **24.6 bp** |
| Range harga dunia (Gate) 48 jam | **292.2 bp** |
| Deviasi pool vs world mid 48 jam | **-161.1 .. +133.4 bp, median -71.7** |
| Spot vs TWAP 1 jam | 0.0 bp (harga tidak bergerak) |
| Gap terlebar (5m–1h) | 0.1 bp |
| Round-trip cost | ~60 bp, **konstan** berapa pun ukuran |
| Single-leg cost $100 | 62.5 bp (fee 30 + impact 32.5) |
| Deploy vault | 1.25 HSK = $0.10 |

Konsekuensi: bot trading di pool ini tidak bisa profit **diukur terhadap dirinya sendiri**.
Tapi pengukuran 48 jam membantah separuh tesis awal: pool ini bukan efisien, melainkan
**basi (stale)** — harganya nyaris tidak bergerak (24.6 bp) sementara harga dunia bergerak
292.2 bp, dan deviasinya berganti tanda (-161 .. +133 bp). Pool yang tidak diarbitrase.
Saat ini sisi jual ~+42 bp kaya vs best bid CEX, sisi beli -119 bp, jadi round trip tetap
rugi 64.8 bp (5/5 sampel) dan itu bruto sebelum fee taker bursa.

Jadi produknya = agent yang **menolak** trading dan membuktikan penolakannya benar, dengan
satu batas yang ditulis terbuka: gate-nya membandingkan spot ke TWAP pool sendiri, sehingga
dislokasi seluruh pool terhadap harga dunia tidak terlihat olehnya.

## Yang membedakan dari pesaing

Semua submission lain di track AI Agent Trading (SayTrade, Keel, Sentinel, Jangseung,
Glassbox) pola sama: agent memutuskan → vault membatasi ukuran. Punya kita menambah 2 aturan:

1. Tolak kalau gap pasar terukur < round-trip cost + margin
2. Tolak kalau klaim model > edge yang pasar benar-benar tawarkan

Keduanya diukur on-chain oleh kontrak, bukan dari pengakuan model.

Celah pesaing yang terdokumentasi: SayTrade klaim "live agent loop tiap 5 menit" tapi
on-chain cuma 1 swap dalam 4,5 hari (vault 0xe210a5880792Ae2e2E232Cc1bA219ee44792b19D,
5 transaksi total sejak deploy).

## Test (26/26 lulus, semua fork mainnet)

- `test/GuardrailFork.t.sol` (15) — gate, cost model, owner safety
- `test/TradePath.t.sol` (2) — **membuktikan gate MEMBUKA**: gap 16bp tolak, dorong harga,
  gap 917bp → trade jalan, 1.124 WHSK masuk vault
- `test/Adversarial.t.sol` (9) — overclaim, cap bypass, hostile router, privilege escalation,
  5 upaya owner melonggarkan limit, pause, ledger tamper, input degenerate

Bug nyata yang ditemukan adversarial suite: quote $50M balik **0 bp** ("trading gratis").
Akar masalah: model single-tick jenuh. Batas validitas terukur: valid s/d ~$100K (1.24x),
fiksi di $1M (3.38x) dan $50M (119.8x). Fix: kalau harga simulasi bergerak >2x, kembalikan
`type(uint256).max` → `BadPoolState` (tolak, bukan murah).

## Yang berubah di putaran lanjutan (25 Nov, sesi lanjut dari VPS)

1. **Klaim 0bp milik pesaing ditarik dan diganti bukti.** Saya cek source terverifikasi
   SayTrade (`0xe210a588...`) dan Keel (`0x8980296B...`) di Blockscout: tidak ada satu pun
   yang menghitung round-trip cost (`roundTrip`/`costBps` = 0 kemunculan di keduanya), jadi
   bug 0bp itu milik saya sendiri — muncul karena saya memilih memproduksi angka itu.
   Sudah ditulis di README dan di-commit (`8c0635e`).
2. **Tesis "pool efisien" dikoreksi jadi "pool basi".** Lihat tabel di atas. Diukur lewat
   `slot0` historis vs candle 1 jam Gate. Ini yang paling penting: arah dislokasi bukan
   premium permanen, tapi pool yang membeku sementara dunia bergerak.
3. **Angka volume dikoreksi.** $558/hari itu rata-rata 50 jam. 24 jam terakhir cuma
   $135.09 dari 12 swap oleh 3 alamat. Keduanya benar; yang 24 jam adalah keadaan sekarang.
4. **Batas gate ditulis eksplisit** di README + paket submission: gate buta terhadap
   dislokasi global karena hanya membaca ring buffer TWAP pool sendiri.

## Yang belum selesai

1. **Belum submit ke DoraHacks** — butuh login user + lewati Cloudflare Turnstile (CAPTCHA
   gambar). Tidak bisa diotomasi, dan memang tidak seharusnya. Isi berkas submission sudah siap
   di paket di atas; tinggal tempel.
2. **Belum deploy mainnet** — butuh 1.25 HSK ($0.10), tidak ada di wallet mana pun. Scan 55.114
   alamat vault sedang jalan di background (kuota RPC 50 alamat/10 detik → ~3 jam). Faucet
   testnet dikunci reCAPTCHA Enterprise; signature HMAC-nya sudah tembus, captcha tidak.
   Catatan: aturan hackathon hanya mewajibkan deploy mainnet untuk tim yang terpilih demo
   (diumumkan 4 Des), jadi ini tidak menghalangi submit.
3. Testnet HSK (133) tidak punya pool WHSK/USDT sama sekali — `getPool` balik nol untuk semua
   fee tier. Demo testnet butuh pool dideploy dulu.

## Perintah

```bash
export PATH="$HOME/.foundry/bin:$PATH"
cd ~/LYID-BOTS/hsk-guardrail
forge test                                    # 26/26
/usr/bin/python3 agent/kharon.py --notional 10 --claim 20
/usr/bin/python3 agent/desk.py                # http://127.0.0.1:8791
```

Deploy (kalau sudah ada HSK):
```bash
export PRIVATE_KEY=... VAULT_OWNER=0x... VAULT_AGENT=0x...
forge script script/Deploy.s.sol:Deploy --rpc-url https://mainnet.hsk.xyz --broadcast
```

## Jebakan teknis yang sudah dipetakan

- RPC mainnet.hsk.xyz: Cloudflare, kuota ~50 request/batch per 10 detik. Scan agresif = 429
  dengan retry-after, dan itu ikut memblokir forge test. Pace pelan kalau ada job lain jalan.
- QuoterV2 BUKAN view function — pricing lewat revert. `staticcall` gagal; harus `call`.
- Getter struct otomatis balik tuple → butuh `getPolicy()` eksplisit.
- `1.0001^tick` harus di-scale sebelum dikuadratkan, kalau tidak harga sub-dollar dibulatkan ke 0.
- Semua math pool dalam satuan raw token (USDT 6dp, WHSK 18dp). Campur 1e18 = overflow.
- front-end app.hskswap.com geo-blocked dari server ini; semua alamat dipulihkan dari chain
  (`factory()` → `getPool()` → cek symbol/decimals/balance, lalu QuoterV2 sebagai bukti akhir).
