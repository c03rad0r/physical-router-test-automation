/**
 * V4 → V3 Token Converter for TollGate Captive Portal
 *
 * The portal already decodes both V3 (cashuA) and V4 (cashuB) tokens for validation.
 * But the Go backend (gonuts) only accepts V3 format. This module converts an already-
 * decoded token back to V3 format before sending to the backend.
 *
 * Usage in the portal's ME() function:
 *
 *   // Before: sends raw token string (V4 fails on Go backend)
 *   const event = { tags: [["payment", rawToken]] };
 *
 *   // After: converts to V3 before sending
 *   const event = { tags: [["payment", ensureV3(rawToken)]] };
 *
 * Where to add this in the portal:
 *   1. In ME() function, replace `i` (the raw token) with `ensureV3(i)` in the payment tag
 *   2. The portal already has the decoded token from DE() validation — you could also pass
 *      the decoded object through to ME() and use `encodeV3Token()` directly, avoiding a
 *      redundant decode.
 */

/**
 * Convert a Cashu token string to V3 format.
 * If the token is already V3 (cashuA prefix), returns it unchanged.
 * If the token is V4 (cashuB prefix), decodes it and re-encodes as V3.
 *
 * @param tokenStr - Raw Cashu token string (cashuA... or cashuB...)
 * @returns V3-encoded Cashu token string (cashuA...)
 */
export function ensureV3(tokenStr: string): string {
  // Strip URI prefixes
  const stripped = stripPrefix(tokenStr);

  // Check version after "cashu" prefix
  if (!stripped.startsWith("cashu")) {
    return tokenStr; // Not a cashu token, return as-is (will fail validation elsewhere)
  }

  const version = stripped[5]; // 'A' or 'B'
  if (version === "A") {
    return tokenStr; // Already V3
  }

  if (version !== "B") {
    return tokenStr; // Unknown version, return as-is
  }

  // Decode V4 → Token object → re-encode as V3
  try {
    const decoded = decodeTokenToObject(stripped);
    return encodeV3Token(decoded);
  } catch (e) {
    console.error("Failed to convert V4 token to V3:", e);
    return tokenStr; // Return original on failure (backend will reject with clear error)
  }
}

// ── Token object structure (already decoded by the portal) ──

interface Proof {
  id: string; // hex keyset ID
  amount: number;
  secret: string;
  C: string; // hex public key
}

interface DecodedToken {
  mint: string;
  proofs: Proof[];
  unit: string;
  memo?: string;
}

// ── V3 Encoder ──

/**
 * Encode a decoded token object as a V3 Cashu token string.
 * V3 format: "cashuA" + base64url(JSON({"token":[{"mint":"...","proofs":[...]}],"unit":"sat","mint":"..."}))
 *
 * Note: The V3 JSON structure wraps proofs in a "token" array where each entry has
 * its own "mint" field, plus a top-level "mint" and "unit".
 */
export function encodeV3Token(token: DecodedToken): string {
  const v3Payload = {
    token: [
      {
        mint: token.mint,
        proofs: token.proofs.map((p) => ({
          id: p.id,
          amount: p.amount,
          secret: p.secret,
          C: p.C,
        })),
      },
    ],
    unit: token.unit || "sat",
    mint: token.mint,
    ...(token.memo && { memo: token.memo }),
  };

  const json = JSON.stringify(v3Payload);
  const base64 = btoa(json);
  // Convert to base64url (replace +/ with -_, strip =)
  const base64url = base64.replace(/\+/g, "-").replace(/\//g, "_").replace(/=+$/, "");

  return "cashuA" + base64url;
}

// ── V4 Decoder (minimal, for conversion only) ──
// The portal already has these functions (iv, W1, nv).
// This is a standalone version that could be used if the portal's internal
// functions aren't accessible from the conversion point.

function stripPrefix(token: string): string {
  const schemes = ["web+cashu://", "cashu://", "cashu:", "cashu"];
  for (const scheme of schemes) {
    if (token.startsWith(scheme)) {
      return "cashu" + token.slice(scheme.length);
    }
  }
  return token;
}

/**
 * Decode a stripped token (after "cashu" prefix removed) into a Token object.
 * Handles both V3 and V4 formats.
 */
function decodeTokenToObject(strippedToken: string): DecodedToken {
  const afterPrefix = strippedToken.slice(5); // remove "cashu"
  const version = afterPrefix[0];
  const payload = afterPrefix.slice(1);

  if (version === "A") {
    return decodeV3(payload);
  } else if (version === "B") {
    return decodeV4(payload);
  }

  throw new Error(`Unsupported token version: ${version}`);
}

function decodeV3(base64urlPayload: string): DecodedToken {
  // base64url → base64 → decode
  let base64 = base64urlPayload.replace(/-/g, "+").replace(/_/g, "/");
  while (base64.length % 4 !== 0) base64 += "=";

  const json = atob(base64);
  const parsed = JSON.parse(json);

  // V3 structure: { token: [{ mint, proofs: [{id, amount, secret, C}] }], unit, mint }
  if (!parsed.token || !Array.isArray(parsed.token) || parsed.token.length === 0) {
    throw new Error("Invalid V3 token: missing token array");
  }

  const entry = parsed.token[0];
  return {
    mint: entry.mint,
    proofs: entry.proofs.map((p: any) => ({
      id: p.id,
      amount: Number(p.amount),
      secret: p.secret,
      C: p.C,
    })),
    unit: parsed.unit || "sat",
    ...(parsed.memo && { memo: parsed.memo }),
  };
}

function decodeV4(base64urlPayload: string): DecodedToken {
  // base64url → bytes → CBOR decode
  let base64 = base64urlPayload.replace(/-/g, "+").replace(/_/g, "/");
  while (base64.length % 4 !== 0) base64 += "=";

  const binary = atob(base64);
  const bytes = new Uint8Array(binary.length);
  for (let i = 0; i < binary.length; i++) bytes[i] = binary.charCodeAt(i);

  const cbor = decodeCBOR(bytes) as V4Template;

  // Extract proofs from V4 template
  const proofs: Proof[] = [];
  for (const entry of cbor.t) {
    const keysetId = bytesToHex(entry.i);
    for (const p of entry.p) {
      proofs.push({
        id: keysetId,
        amount: Number(p.a),
        secret: p.s,
        C: bytesToHex(p.c),
      });
    }
  }

  return {
    mint: cbor.m,
    proofs,
    unit: cbor.u || "sat",
    ...(cbor.d && { memo: cbor.d }),
  };
}

// ── Minimal CBOR decoder ──
// The portal already has W1() for this. Including a standalone version
// in case the portal's internal CBOR decoder isn't accessible.
// ~50 lines. Handles only the CBOR subset used by Cashu V4 tokens.

interface V4Proof {
  a: bigint | number; // amount
  s: string; // secret
  c: Uint8Array; // commitment (public key)
}

interface V4TokenEntry {
  i: Uint8Array; // keyset ID
  p: V4Proof[];
}

interface V4Template {
  m: string; // mint URL
  u: string; // unit
  t: V4TokenEntry[];
  d?: string; // memo
}

function decodeCBOR(data: Uint8Array): any {
  const view = new DataView(data.buffer, data.byteOffset, data.byteLength);
  let offset = 0;

  function read(): any {
    if (offset >= view.byteLength) throw new Error("CBOR: unexpected end of data");
    const header = view.getUint8(offset++);
    const majorType = header >> 5;
    const additionalInfo = header & 0x1f;

    switch (majorType) {
      case 0: // unsigned integer
        return readUint(additionalInfo);
      case 1: // negative integer
        return -(readUint(additionalInfo) + 1);
      case 2: // byte string
        return readBytes(additionalInfo);
      case 3: // text string
        return readText(additionalInfo);
      case 4: // array
        return readArray(additionalInfo);
      case 5: // map
        return readMap(additionalInfo);
      default:
        throw new Error(`CBOR: unsupported major type ${majorType} at offset ${offset - 1}`);
    }
  }

  function readUint(info: number): number {
    if (info < 24) return info;
    if (info === 24) return view.getUint8(offset++);
    if (info === 25) {
      const val = view.getUint16(offset);
      offset += 2;
      return val;
    }
    if (info === 26) {
      const val = view.getUint32(offset);
      offset += 4;
      return val;
    }
    throw new Error(`CBOR: unsupported uint info ${info}`);
  }

  function readLength(info: number): number {
    if (info < 24) return info;
    if (info === 24) return view.getUint8(offset++);
    if (info === 25) {
      const val = view.getUint16(offset);
      offset += 2;
      return val;
    }
    if (info === 26) {
      const val = view.getUint32(offset);
      offset += 4;
      return val;
    }
    if (info === 31) return -1; // indefinite
    throw new Error(`CBOR: unsupported length info ${info}`);
  }

  function readBytes(info: number): Uint8Array {
    const len = readLength(info);
    if (len < 0) throw new Error("CBOR: indefinite byte strings not supported");
    const bytes = new Uint8Array(data.buffer, data.byteOffset + offset, len);
    offset += len;
    return bytes;
  }

  function readText(info: number): string {
    const len = readLength(info);
    if (len < 0) throw new Error("CBOR: indefinite text strings not supported");
    const bytes = new Uint8Array(data.buffer, data.byteOffset + offset, len);
    offset += len;
    return new TextDecoder().decode(bytes);
  }

  function readArray(info: number): any[] {
    const len = readLength(info);
    if (len < 0) throw new Error("CBOR: indefinite arrays not supported");
    const arr: any[] = [];
    for (let i = 0; i < len; i++) arr.push(read());
    return arr;
  }

  function readMap(info: number): Record<string, any> {
    const len = readLength(info);
    if (len < 0) throw new Error("CBOR: indefinite maps not supported");
    const map: Record<string, any> = {};
    for (let i = 0; i < len; i++) {
      const key = read();
      const value = read();
      map[key] = value;
    }
    return map;
  }

  return read();
}

function bytesToHex(bytes: Uint8Array): string {
  return Array.from(bytes)
    .map((b) => b.toString(16).padStart(2, "0"))
    .join("");
}
