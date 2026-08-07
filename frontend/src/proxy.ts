import { type NextRequest, NextResponse } from "next/server";

import {
  AUTH_COOKIE_NAME,
  authenticationEnabled,
  validSessionToken,
} from "@/lib/server-auth";

function safeDestination(request: NextRequest) {
  const candidate = request.nextUrl.searchParams.get("next") || "/";
  return candidate.startsWith("/") && !candidate.startsWith("//")
    ? candidate
    : "/";
}

export function proxy(request: NextRequest) {
  const loginPage = request.nextUrl.pathname === "/login";
  if (!authenticationEnabled()) {
    return NextResponse.next();
  }

  const authenticated = validSessionToken(
    request.cookies.get(AUTH_COOKIE_NAME)?.value,
  );
  if (loginPage) {
    return authenticated
      ? NextResponse.redirect(new URL(safeDestination(request), request.url))
      : NextResponse.next();
  }
  if (authenticated) return NextResponse.next();

  const loginUrl = new URL("/login", request.url);
  loginUrl.searchParams.set(
    "next",
    `${request.nextUrl.pathname}${request.nextUrl.search}`,
  );
  return NextResponse.redirect(loginUrl);
}

export const config = {
  matcher: [
    "/((?!api|_next/static|_next/image|favicon.ico|sitemap.xml|robots.txt|.*\\..*$).*)",
  ],
};
