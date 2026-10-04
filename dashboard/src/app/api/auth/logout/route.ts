import { NextResponse } from "next/server";

import { destroySession } from "@/lib/session";

export const dynamic = "force-dynamic";

/** Sign out. POST-only so a stray <img src> cannot log a user out. */
export async function POST(request: Request): Promise<NextResponse> {
  await destroySession();
  return NextResponse.redirect(new URL("/", request.url), { status: 303 });
}