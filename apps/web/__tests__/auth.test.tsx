import { afterEach, beforeEach, describe, expect, it, vi } from "vitest";
import { fireEvent, render, screen, waitFor } from "@testing-library/react";
import { CSRF_COOKIE, CSRF_HEADER, api, readCsrfToken } from "@/lib/api";

const replace = vi.fn();

vi.mock("next/navigation", () => ({
  useRouter: () => ({ replace, push: vi.fn() }),
  usePathname: () => "/resume-studio",
  useSearchParams: () => new URLSearchParams(""),
}));

function jsonResponse(body: unknown, status = 200): Response {
  return {
    ok: status >= 200 && status < 300,
    status,
    text: async () => JSON.stringify(body),
  } as Response;
}

/**
 * Route the stub by URL.
 *
 * The login page probes `/auth/config` before rendering, so a single
 * catch-all stub would answer that probe with a login payload and the form
 * would render the wrong fields.
 */
function routedFetch(routes: {
  config?: { totp_required: boolean };
  login?: [unknown, number];
  me?: [unknown, number];
}) {
  return vi.fn(async (url: string) => {
    if (url.includes("/auth/config")) {
      return jsonResponse(routes.config ?? { totp_required: true });
    }
    if (url.includes("/auth/login")) {
      const [body, status] = routes.login ?? [{ detail: "no" }, 401];
      return jsonResponse(body, status);
    }
    const [body, status] = routes.me ?? [{ detail: "no" }, 401];
    return jsonResponse(body, status);
  });
}

const LOGIN_OK: [unknown, number] = [
  { user: { id: "u1", email: "demo.candidate@example.com", role: "owner" }, csrf_token: "t" },
  200,
];

function lastRequestInit(): RequestInit {
  const mock = globalThis.fetch as unknown as {
    mock: { calls: [string, RequestInit][] };
  };
  const calls = mock.mock.calls;
  const call = calls[calls.length - 1];
  if (!call) throw new Error("expected fetch to have been called");
  return call[1];
}

beforeEach(() => {
  replace.mockClear();
  document.cookie = `${CSRF_COOKIE}=csrf-token-value`;
});

afterEach(() => {
  vi.unstubAllGlobals();
  document.cookie = `${CSRF_COOKIE}=; expires=Thu, 01 Jan 1970 00:00:00 GMT`;
});

describe("API client credentials and CSRF", () => {
  it("reads the CSRF cookie the API set at login", () => {
    expect(readCsrfToken()).toBe("csrf-token-value");
  });

  it("sends cookies on every request", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ id: "u1" })));
    await api.me();
    expect(lastRequestInit().credentials).toBe("include");
  });

  it("attaches the CSRF header to mutating requests", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ revoked: true })));
    await api.logout();
    const headers = lastRequestInit().headers as Record<string, string>;
    expect(headers[CSRF_HEADER]).toBe("csrf-token-value");
  });

  it("does not attach the CSRF header to reads", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ id: "u1" })));
    await api.me();
    const headers = lastRequestInit().headers as Record<string, string>;
    expect(headers[CSRF_HEADER]).toBeUndefined();
  });

  it("surfaces a 401 as an ApiError rather than a silent null", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ detail: "no" }, 401)));
    await expect(api.me()).rejects.toMatchObject({ status: 401 });
  });
});

describe("login page", () => {
  async function renderLogin(config: { totp_required: boolean } = { totp_required: true }) {
    vi.stubGlobal("fetch", routedFetch({ config }));
    const { default: Page } = await import("@/app/login/page");
    render(<Page />);
    await screen.findByRole("button", { name: /sign in/i });
    // Let the /auth/config probe settle before asserting on the fields.
    if (config.totp_required) {
      await screen.findByLabelText("Authenticator code");
    } else {
      await waitFor(() =>
        expect(screen.queryByLabelText("Authenticator code")).toBeNull(),
      );
    }
  }

  async function fill(withCode: boolean) {
    fireEvent.change(screen.getByLabelText("Email"), {
      target: { value: "demo.candidate@example.com" },
    });
    fireEvent.change(screen.getByLabelText("Password"), {
      target: { value: "pw" },
    });
    if (withCode) {
      fireEvent.change(screen.getByLabelText("Authenticator code"), {
        target: { value: "123456" },
      });
    }
  }

  it("renders all three credential fields when MFA is required", async () => {
    await renderLogin();
    expect(screen.getByLabelText("Email")).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toBeInTheDocument();
    expect(screen.getByLabelText("Authenticator code")).toBeInTheDocument();
  });

  it("hides the authenticator field when the deployment does not require it", async () => {
    await renderLogin({ totp_required: false });
    expect(screen.getByLabelText("Email")).toBeInTheDocument();
    expect(screen.getByLabelText("Password")).toBeInTheDocument();
    expect(screen.queryByLabelText("Authenticator code")).toBeNull();
    expect(screen.getByText(/your password is enough/i)).toBeInTheDocument();
  });

  it("keeps submit disabled until every required field is filled", async () => {
    await renderLogin();
    const submit = screen.getByRole("button", { name: /sign in/i });
    expect(submit).toBeDisabled();
    await fill(false);
    expect(submit).toBeDisabled(); // still missing the second factor
    await fill(true);
    expect(submit).toBeEnabled();
  });

  it("enables submit without a code when MFA is not required", async () => {
    await renderLogin({ totp_required: false });
    const submit = screen.getByRole("button", { name: /sign in/i });
    expect(submit).toBeDisabled();
    await fill(false);
    expect(submit).toBeEnabled();
  });

  it("shows one indistinguishable message for bad credentials", async () => {
    await renderLogin();
    vi.stubGlobal("fetch", routedFetch({ login: [{ detail: "no" }, 401] }));
    await fill(true);
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));
    expect(await screen.findByRole("alert")).toHaveTextContent(
      "Invalid email, password or code.",
    );
  });

  it("redirects after a successful sign-in", async () => {
    await renderLogin();
    vi.stubGlobal("fetch", routedFetch({ login: LOGIN_OK }));
    await fill(true);
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));
    await waitFor(() => expect(replace).toHaveBeenCalledWith("/"));
  });

  it("posts both factors together in one request", async () => {
    await renderLogin();
    vi.stubGlobal("fetch", routedFetch({ login: LOGIN_OK }));
    await fill(true);
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));
    await waitFor(() => expect(replace).toHaveBeenCalled());
    const body = JSON.parse(String(lastRequestInit().body));
    expect(body).toMatchObject({
      email: "demo.candidate@example.com",
      password: "pw",
      totp_code: "123456",
    });
  });

  it("omits totp_code entirely when no code was entered", async () => {
    await renderLogin({ totp_required: false });
    vi.stubGlobal("fetch", routedFetch({ config: { totp_required: false }, login: LOGIN_OK }));
    await fill(false);
    fireEvent.click(screen.getByRole("button", { name: /sign in/i }));
    await waitFor(() => expect(replace).toHaveBeenCalled());
    const body = JSON.parse(String(lastRequestInit().body));
    expect(body).toEqual({ email: "demo.candidate@example.com", password: "pw" });
  });

  it("keeps the strict form when the policy probe fails", async () => {
    // Failing closed: if we cannot learn the policy, ask for the code.
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ detail: "x" }, 500)));
    const { default: Page } = await import("@/app/login/page");
    render(<Page />);
    expect(
      await screen.findByLabelText("Authenticator code"),
    ).toBeInTheDocument();
  });
});

describe("auth guard", () => {
  async function renderGuard() {
    const { AuthGuard } = await import("@/components/AuthGuard");
    render(
      <AuthGuard>
        <p>protected content</p>
      </AuthGuard>,
    );
  }

  it("redirects to login when the session is missing", async () => {
    vi.stubGlobal("fetch", vi.fn(async () => jsonResponse({ detail: "no" }, 401)));
    await renderGuard();
    await waitFor(() =>
      expect(replace).toHaveBeenCalledWith("/login?next=%2Fresume-studio"),
    );
    expect(screen.queryByText("protected content")).not.toBeInTheDocument();
  });

  it("renders protected content for an authenticated user", async () => {
    vi.stubGlobal(
      "fetch",
      vi.fn(async () =>
        jsonResponse({ id: "u1", email: "demo@example.invalid", role: "owner" }),
      ),
    );
    await renderGuard();
    expect(await screen.findByText("protected content")).toBeInTheDocument();
    expect(replace).not.toHaveBeenCalled();
  });

  it("shows a checking state before the session is known", async () => {
    vi.stubGlobal("fetch", vi.fn(() => new Promise(() => {})));
    await renderGuard();
    expect(screen.getByTestId("auth-loading")).toBeInTheDocument();
  });
});
