import { createHmac, timingSafeEqual } from "node:crypto";

export const AUTH_COOKIE_NAME = "article_agent_session";

export function authenticationEnabled() {
  return Boolean(process.env.APP_PASSWORD?.trim());
}

function sessionSecret() {
  return (
    process.env.APP_SESSION_SECRET?.trim() ||
    process.env.APP_PASSWORD?.trim() ||
    ""
  );
}

function signature(expiresAt: number) {
  return createHmac("sha256", sessionSecret())
    .update(String(expiresAt), "ascii")
    .digest("base64url");
}

export function validSessionToken(token: string | undefined) {
  if (!authenticationEnabled() || !token) return false;
  const [expiresRaw, receivedSignature, ...extra] = token.split(".");
  if (!expiresRaw || !receivedSignature || extra.length) return false;
  const expiresAt = Number(expiresRaw);
  if (!Number.isSafeInteger(expiresAt) || expiresAt <= Math.floor(Date.now() / 1000)) {
    return false;
  }
  const expectedSignature = signature(expiresAt);
  const received = Buffer.from(receivedSignature);
  const expected = Buffer.from(expectedSignature);
  return (
    received.length === expected.length &&
    timingSafeEqual(received, expected)
  );
}
