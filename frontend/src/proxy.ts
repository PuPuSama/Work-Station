import { type NextRequest, NextResponse } from "next/server";

const SERVER_SESSION_COOKIE = "article_agent_actor_session";

function safeDestination(request: NextRequest) {
  const candidate = request.nextUrl.searchParams.get("next") || "/";
  return candidate.startsWith("/") && !candidate.startsWith("//")
    ? candidate
    : "/";
}

export function proxy(request: NextRequest) {
  const pathname = request.nextUrl.pathname;
  const authenticated = Boolean(
    request.cookies.get(SERVER_SESSION_COOKIE)?.value,
  );

  if (pathname === "/login") {
    return authenticated
      ? NextResponse.redirect(new URL(safeDestination(request), request.url))
      : NextResponse.next();
  }
  if (pathname === "/accept-invite") return NextResponse.next();
  if (authenticated) return NextResponse.next();

  const loginUrl = new URL("/login", request.url);
  loginUrl.searchParams.set(
    "next",
    `${request.nextUrl.pathname}${request.nextUrl.search}`,
  );
  // The backend verifies the signed cookie; this edge check only prevents
  // loading protected pages before the API can return its 401 response.
  return NextResponse.redirect(loginUrl);
}

export const config = {
  matcher: [
    "/((?!api|_next/static|_next/image|favicon.ico|sitemap.xml|robots.txt|.*\\..*$).*)",
  ],
};
