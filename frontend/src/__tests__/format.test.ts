import { describe, it, expect, afterEach, vi } from "vitest";
import type { Instance } from "../api/types";
import {
  errMsg,
  fmtTokens,
  fmtUsd,
  provLabel,
  relTime,
  fmtDurationShort,
  displayBranch,
  humanSize,
  pathBasename,
} from "../lib/format";

/** displayBranch only reads branch/program; build a loose row. */
const row = (o: Partial<Instance>): Instance => o as unknown as Instance;

describe("errMsg", () => {
  it("returns the message of an Error", () => {
    expect(errMsg(new Error("boom"))).toBe("boom");
  });
  it("reads .message off any object with one", () => {
    expect(errMsg({ message: "custom" })).toBe("custom");
  });
  it("falls back to 'error' for null/undefined and messageless values", () => {
    expect(errMsg(null)).toBe("error");
    expect(errMsg(undefined)).toBe("error");
    expect(errMsg("a string")).toBe("error");
    expect(errMsg(42)).toBe("error");
  });
  it("falls back when the message is empty", () => {
    expect(errMsg(new Error(""))).toBe("error");
    expect(errMsg({ message: "" })).toBe("error");
  });
});

describe("fmtTokens", () => {
  it("coerces null/undefined/0 to '0'", () => {
    expect(fmtTokens(0)).toBe("0");
    expect(fmtTokens(null)).toBe("0");
    expect(fmtTokens(undefined)).toBe("0");
  });
  it("shows raw counts under 1000", () => {
    expect(fmtTokens(1)).toBe("1");
    expect(fmtTokens(999)).toBe("999");
  });
  it("uses one decimal for k below 10k, none at/above", () => {
    expect(fmtTokens(1000)).toBe("1.0k");
    expect(fmtTokens(1500)).toBe("1.5k");
    expect(fmtTokens(9999)).toBe("10.0k"); // rounds up but still <10k branch
    expect(fmtTokens(10000)).toBe("10k");
    expect(fmtTokens(128000)).toBe("128k");
    expect(fmtTokens(999999)).toBe("1000k");
  });
  it("switches to M at a million", () => {
    expect(fmtTokens(1e6)).toBe("1.0M");
    expect(fmtTokens(2_500_000)).toBe("2.5M");
  });
  it("passes small negatives through as raw numbers", () => {
    expect(fmtTokens(-5)).toBe("-5");
  });
});

describe("fmtUsd", () => {
  it("shows $0 for zero, null, and non-positive input", () => {
    expect(fmtUsd(0)).toBe("$0");
    expect(fmtUsd(null)).toBe("$0");
    expect(fmtUsd(undefined)).toBe("$0");
    expect(fmtUsd(-5)).toBe("$0");
  });
  it("collapses tiny positives to <$0.01", () => {
    expect(fmtUsd(0.005)).toBe("<$0.01");
    expect(fmtUsd(0.009999)).toBe("<$0.01");
  });
  it("uses cents below $10", () => {
    expect(fmtUsd(0.01)).toBe("$0.01");
    expect(fmtUsd(0.62)).toBe("$0.62");
    expect(fmtUsd(9.999)).toBe("$10.00"); // rounds within the <$10 branch
  });
  it("uses one decimal from $10 to <$1000", () => {
    expect(fmtUsd(10)).toBe("$10.0");
    expect(fmtUsd(500)).toBe("$500.0");
  });
  it("switches to k at $1000", () => {
    expect(fmtUsd(1000)).toBe("$1.0k");
    expect(fmtUsd(2500)).toBe("$2.5k");
  });
});

describe("provLabel", () => {
  it("uses the known label map", () => {
    expect(provLabel("claude")).toBe("Claude");
    expect(provLabel("codex")).toBe("Codex");
    expect(provLabel("aider")).toBe("Aider");
  });
  it("capitalizes unknown provider names", () => {
    expect(provLabel("gemini")).toBe("Gemini");
    expect(provLabel("x")).toBe("X");
  });
  it("returns '' for empty/null/undefined", () => {
    expect(provLabel("")).toBe("");
    expect(provLabel(null)).toBe("");
    expect(provLabel(undefined)).toBe("");
  });
});

describe("relTime", () => {
  const NOW_MS = 1_700_000_000_000;
  const NOW_SEC = NOW_MS / 1000;
  afterEach(() => vi.restoreAllMocks());
  const at = () => vi.spyOn(Date, "now").mockReturnValue(NOW_MS);

  it("floors a future or current timestamp to '0s ago'", () => {
    at();
    expect(relTime(NOW_SEC)).toBe("0s ago");
    expect(relTime(NOW_SEC + 100)).toBe("0s ago");
  });
  it("uses seconds below a minute", () => {
    at();
    expect(relTime(NOW_SEC - 30)).toBe("30s ago");
    expect(relTime(NOW_SEC - 59)).toBe("59s ago");
  });
  it("uses minutes below an hour", () => {
    at();
    expect(relTime(NOW_SEC - 60)).toBe("1m ago");
    expect(relTime(NOW_SEC - 3599)).toBe("59m ago");
  });
  it("uses hours below a day", () => {
    at();
    expect(relTime(NOW_SEC - 3600)).toBe("1h ago");
    expect(relTime(NOW_SEC - 86399)).toBe("23h ago");
  });
  it("uses days beyond that", () => {
    at();
    expect(relTime(NOW_SEC - 86400)).toBe("1d ago");
    expect(relTime(NOW_SEC - 3 * 86400)).toBe("3d ago");
  });
});

describe("fmtDurationShort", () => {
  it("clamps to a 1m floor and rounds to the nearest minute", () => {
    expect(fmtDurationShort(0)).toBe("~1m");
    expect(fmtDurationShort(29_000)).toBe("~1m");
    expect(fmtDurationShort(60_000)).toBe("~1m");
    expect(fmtDurationShort(90_000)).toBe("~2m"); // 1.5m rounds up
    expect(fmtDurationShort(7 * 60_000)).toBe("~7m");
  });
  it("splits into hours and minutes at/above an hour", () => {
    expect(fmtDurationShort(60 * 60_000)).toBe("1h 0m");
    expect(fmtDurationShort((2 * 60 + 11) * 60_000)).toBe("2h 11m");
  });
});

describe("displayBranch", () => {
  it("extracts the descriptive slug from a Shortcut branch", () => {
    expect(displayBranch(row({ branch: "feature/sc-19827/scan-sms-flow" }))).toBe("scan-sms-flow");
  });
  it("extracts the slug from a mindflock/ branch", () => {
    expect(displayBranch(row({ branch: "mindflock/foo-bar" }))).toBe("foo-bar");
  });
  it("shows the full branch when no known prefix matches", () => {
    expect(displayBranch(row({ branch: "main" }))).toBe("main");
    // A trailing slash leaves the slug capture empty, so no prefix matches.
    expect(displayBranch(row({ branch: "feature/sc-1/" }))).toBe("feature/sc-1/");
  });
  it("falls back to program, then '', when there is no branch", () => {
    expect(displayBranch(row({ program: "prog" }))).toBe("prog");
    expect(displayBranch(row({}))).toBe("");
  });
});

describe("humanSize", () => {
  it("uses bytes below 1 KiB", () => {
    expect(humanSize(0)).toBe("0 B");
    expect(humanSize(512)).toBe("512 B");
    expect(humanSize(1023)).toBe("1023 B");
  });
  it("uses KB up to 1 MiB", () => {
    expect(humanSize(1024)).toBe("1.0 KB");
    expect(humanSize(1536)).toBe("1.5 KB");
  });
  it("uses MB up to 1 GiB", () => {
    expect(humanSize(1024 * 1024)).toBe("1.0 MB");
    expect(humanSize(1536 * 1024)).toBe("1.5 MB");
  });
  it("uses GB with two decimals beyond that", () => {
    expect(humanSize(1024 * 1024 * 1024)).toBe("1.00 GB");
    expect(humanSize(5 * 1024 * 1024 * 1024)).toBe("5.00 GB");
  });
});

describe("pathBasename", () => {
  it("returns the last path segment", () => {
    expect(pathBasename("/a/b/c.txt")).toBe("c.txt");
    expect(pathBasename("file.txt")).toBe("file.txt");
  });
  it("handles Windows backslash separators", () => {
    expect(pathBasename("C:\\Users\\x\\file.js")).toBe("file.js");
  });
  it("strips trailing slashes before taking the basename", () => {
    expect(pathBasename("/a/b/")).toBe("b");
    expect(pathBasename("a/b//")).toBe("b");
    expect(pathBasename("dir/")).toBe("dir");
  });
  it("returns '' for empty, root, and null-ish input", () => {
    expect(pathBasename("")).toBe("");
    expect(pathBasename("/")).toBe("");
    expect(pathBasename(null as unknown as string)).toBe("");
  });
});
