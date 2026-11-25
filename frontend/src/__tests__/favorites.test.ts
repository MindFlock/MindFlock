import { describe, expect, it } from "vitest";
import { favoriteMenuLabel, favoriteStateText, sortFavorites, type FavoriteRow } from "../lib/favorites";
import MENU_SRC from "../lib/sessionMenu.ts?raw";
import ROW_SRC from "../components/sidebar/SidebarRow.tsx?raw";
import BAR_SRC from "../components/sidebar/Sidebar.tsx?raw";
import APP_SRC from "../App.tsx?raw";
import STORE_SRC from "../state/store.ts?raw";
import DLG_SRC from "../components/dialogs/FavoritesDialog.tsx?raw";

describe("favorites", () => {
  it("lists open first, then closed, then gone, each by name", () => {
    const rows: FavoriteRow[] = [
      { title: "zeta", state: "closed" },
      { title: "gone-one", state: "gone" },
      { title: "beta", state: "open" },
      { title: "alpha", state: "closed" },
      { title: "omega", state: "open" },
    ];
    expect(sortFavorites(rows, (t) => t).map((r) => r.title)).toEqual(["beta", "omega", "alpha", "zeta", "gone-one"]);
  });

  it("says what each row is", () => {
    expect(favoriteStateText({ title: "a", state: "open" })).toBe("open");
    expect(favoriteStateText({ title: "a", state: "closed" })).toBe("closed");
    expect(favoriteStateText({ title: "a", state: "gone", wiped: true })).toBe("deleted");
    expect(favoriteStateText({ title: "a", state: "gone" })).toBe("not found");
  });

  it("the menu item reads as a switch", () => {
    expect(favoriteMenuLabel({ favorite: true })).toBe("★ In favorites");
    expect(favoriteMenuLabel({})).toBe("☆ Add to favorites");
  });

  it("is wired: menu item, row star, footer link beside Recently closed, dialog mounted", () => {
    const menu = MENU_SRC;
    expect(menu).toContain('key: "favorite"');
    expect(menu).toMatch(/setFavorite\(title, !inst\.favorite\)/);
    const row = ROW_SRC;
    expect(row).toMatch(/inst\.favorite && \(\s*<span className="fav-mark"/);
    const bar = BAR_SRC;
    expect(bar).toMatch(/id="favorites-btn"[\s\S]*id="recent-btn"/);
    expect(bar).toContain('ui.openDialogFor("favorites")');
    expect(APP_SRC).toContain("<FavoritesDialog />");
    expect(STORE_SRC).toContain('| "favorites"');
    const dlg = DLG_SRC;
    expect(dlg).toContain("/api/recently-closed/");
    expect(dlg).toContain('"/api/favorites"');
  });
});
