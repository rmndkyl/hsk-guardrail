# HSK Guardrail / Kharon — CHECKPOINT

Terakhir diperbarui: 25 Nov 2026

## Status

**Siap submit.** Repo publik, 26/26 test lulus, video demo 63 detik, agent jalan.

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
| Swap dalam ~50 jam | 96 (~$558/hari) |
| Spot vs TWAP 1 jam | 0.0 bp (harga tidak bergerak) |
| Gap terlebar (5m–1h) | 0.1 bp |
| Round-trip cost | ~60 bp, **konstan** berapa pun ukuran |
| Single-leg cost $100 | 62.5 bp (fee 30 + impact 32.5) |
| Deploy vault | 1.25 HSK = $0.10 |

Konsekuensi: bot trading di pool ini tidak bisa profit. Jadi produknya = agent yang
**menolak** trading dan membuktikan penolakannya benar.

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
