"""Check port tile geometry and LLDP labels on the live and in-progress grids."""

import re
import sys
from threading import Event

from browser_lldp_identity import NeighborSwitch
from browser_power_matrix import PowerMatrixSwitch
from browser_qa import (
    enter_ssh_credentials,
    expect_hidden_selection,
    open_port_controls,
    power_filter,
    run_browser_case,
)
from browser_unified_controls import resize_workspace
from playwright.sync_api import expect

LONG_NAME = "본관-3층-통합운영회의실-east-wing-AP-023"


class GridSwitch(NeighborSwitch):
    def __init__(self):
        super().__init__()
        self.draws.update({str(port): 8 for port in range(15, 20)})
        self.neighbors.update({str(port): [] for port in range(15, 20)})
        self.neighbors["7"] = [(LONG_NAME, "Aruba AP-535", "")]
        self.neighbors["8"] = [("i-WW", "ArubaOS", "bridge")]
        self.release_read = Event()
        self.release_read.set()

    def _respond(self, channel, command):
        if command == "show lldp info remote-device 13":
            self.release_read.wait(35)
        return super()._respond(channel, command)


def stacking_switch():
    switch = PowerMatrixSwitch()
    switch.draws = {"1/1": 8}
    return switch


def grid_geometry(tiles, *, columns=None, touch=False):
    boxes = tiles.evaluate_all("""items => items.map(item => {
        const box = item.getBoundingClientRect();
        const port = item.querySelector('.tile-port');
        const label = port && port.getBoundingClientRect();
        const link = item.querySelector('.tile-link').getBoundingClientRect();
        return {width:box.width, height:box.height, left:box.left, top:box.top,
            number: label && (label.top + label.bottom) / 2 - box.top,
            link: link.top - box.top, bottom: link.bottom - box.bottom};
    })""")
    assert boxes, "Expected a nonempty switch grid"
    for key in ("width", "height", "number", "link"):
        values = [b[key] for b in boxes]
        assert all(v is not None for v in values), (key, boxes)
        assert max(values) - min(values) <= 1.1, (key, values)
    assert max(b["bottom"] for b in boxes) <= 1
    if touch:
        assert all(min(b["width"], b["height"]) >= 44 for b in boxes), boxes
    if columns:
        for i, box in enumerate(boxes):
            row_start = boxes[i // columns * columns]
            assert abs(box["top"] - row_start["top"]) < 1, (columns, i, boxes)
            assert abs(box["left"] - boxes[i % columns]["left"]) < 1, (columns, i, boxes)
            if i >= columns:
                assert box["top"] > boxes[i - columns]["top"] + box["height"] - 1, boxes
            elif i:
                assert box["left"] > boxes[i - 1]["left"] + box["width"] - 1, boxes
    return boxes[0]


def check_name(tile, caption_class, name, *, truncated=False):
    caption = tile.locator("." + caption_class)
    expect(caption).to_have_text(name)
    expect(tile).to_have_attribute("title", re.compile(re.escape(name)))
    expect(tile).to_have_accessible_name(re.compile(re.escape(name)))
    if truncated:
        tail = caption.locator(".tile-name-tail")
        expect(tail).to_have_text(re.compile(r"AP-023$"))
        assert caption.evaluate("""caption => {
            const head = caption.querySelector('.tile-name-head');
            const tail = caption.querySelector('.tile-name-tail');
            const range = document.createRange();
            const text = tail.firstChild;
            range.setStart(text, text.length - 'AP-023'.length);
            range.setEnd(text, text.length);
            const suffix = range.getBoundingClientRect();
            const face = caption.getBoundingClientRect();
            const end = tail.getBoundingClientRect();
            return head.scrollWidth > head.clientWidth && head.clientWidth > 0 &&
                getComputedStyle(head).textOverflow === 'ellipsis' && suffix.width > 0 &&
                suffix.left >= Math.max(face.left, end.left) - 1 &&
                suffix.right <= Math.min(face.right, end.right) + 1;
        }""")
    else:
        # A flex-end suffix can clip at its left edge even if scrollWidth == clientWidth.
        clipped = caption.evaluate("""el => {
            const range = document.createRange();
            return [...el.querySelectorAll('.tile-name-head,.tile-name-tail')].flatMap(part => {
                range.selectNodeContents(part);
                const text = range.getBoundingClientRect();
                for (let node = part; node && el.contains(node); node = node.parentElement) {
                    const box = node.getBoundingClientRect();
                    if (text.left < box.left - 1 || text.right > box.right + 1)
                        return [{part:part.textContent, text:text.width, container:box.width}];
                }
                return [];
            });
        }""")
        assert not clipped, (name, clipped)


def exercise(page, url, switch, second, shots, _backup):
    mobile = page.context.browser.browser_type.name == "webkit"
    width, height = (390, 844) if mobile else (1440, 900)
    resize_workspace(page, width, height)
    page.goto(url)
    address, other = [f"{peer.ip}:{peer.port}" for peer in (switch, second)]
    page.locator("#queryTargets").fill(f"{address} 6-19\n{other} 1/1")
    page.locator("#queryPorts").click()
    enter_ssh_credentials(page, switch, touch=mobile)
    frame = page.frame_locator("#matrixViewer")
    tiles = frame.locator(f'#matrix [data-port-key^="{address}/"]')
    stacked = frame.locator(f'#matrix [data-port-key="{other}/1/1"]')
    expect(tiles).to_have_count(14, timeout=90000)
    expect(stacked).to_be_visible()

    def tile(port):
        return frame.locator(f'#matrix [data-port-key="{address}/{port}"]')

    def activate(button):
        if mobile:
            button.tap()
        else:
            button.click()

    tile7, multi, blank = tile(7), tile(10), tile(13)
    layouts = (
        [(width, height, 4), (320, 568, 4)]
        if mobile
        else [(width, height, 12), (1154, 900, 12), (768, 800, 8)]
    )
    for target_width, target_height, columns in layouts:
        resize_workspace(page, target_width, target_height)
        size = grid_geometry(tiles, columns=columns, touch=mobile)
        single = grid_geometry(stacked, touch=mobile)
        assert abs(size["width"] - single["width"]) <= 1, (size, single)
        assert abs(size["height"] - single["height"]) <= 1
        expect(stacked.locator(".tile-port")).to_have_text("1/1")
        expect(
            blank.locator(".tile-ap-name,.tile-neighbor-name,.tile-neighbor-count")
        ).to_have_count(0)
        check_name(tile7, "tile-ap-name", LONG_NAME, truncated=True)
        check_name(tile(6), "tile-ap-name", "AP-SW-07")
        check_name(tile(8), "tile-neighbor-name", "i-WW")
        expect(multi.locator(".tile-neighbor-count")).to_have_text("이웃 2대")
        expect(tile(14).locator(".port-failure-mark")).to_be_visible()
        expect(tile(12)).to_have_attribute("data-state", "unpowered")
        expect(tile(12).locator(".tile-link")).to_have_text("↑ Up")
        assert frame.locator("html").evaluate("el => el.scrollWidth <= el.clientWidth + 1")
        assert page.evaluate("document.documentElement.scrollWidth <= innerWidth")
        if shots:
            tiles.first.scroll_into_view_if_needed()
            page.screenshot(path=shots / f"uniform-grid-{target_width}.png")
        print(
            f"Live {target_width}px: {columns} columns, {size['width']:.2f} x {size['height']:.2f}px"
        )

    resize_workspace(page, width, height)
    columns = layouts[0][2]
    activate(tile7)
    expect(frame.locator("#detail .ap-name")).to_contain_text(LONG_NAME)
    activate(multi)
    facts = frame.locator("#detail .port-facts")
    if not facts.evaluate("el => el.open"):
        activate(facts.get_by_text("포트 상세 정보", exact=True))
    names = facts.get_by_text("LLDP 이웃 이름", exact=True).locator("..").locator(".data")
    for name in ("east-1", "east-2"):
        expect(names).to_contain_text(name)
    full_width = grid_geometry(tiles)["width"]
    select_red = power_filter(frame, "unpowered")
    activate(select_red)
    expect(tiles).to_have_count(1)
    expect_hidden_selection(page, 2)
    assert abs(grid_geometry(tiles)["width"] - full_width) <= 1
    activate(select_red)
    for current in (tile7, multi):
        expect(current).to_have_attribute("aria-pressed", "true")
    slider = frame.locator("#zoom")
    initial = slider.input_value()
    original = grid_geometry(tiles, columns=columns)
    assert abs(original["height"] - int(initial)) < 1, (original, initial)
    expect(frame.locator("#zoomValue")).to_have_text(initial + "px")
    slider.focus()
    slider.press("End")
    expect(slider).not_to_have_value(initial)
    enlarged = grid_geometry(tiles, touch=mobile)
    assert enlarged["height"] > original["height"]
    assert abs(enlarged["height"] - int(slider.input_value())) < 1
    expect(frame.locator("#zoomValue")).to_have_text(slider.input_value() + "px")
    for current in (tile7, multi):
        expect(current).to_have_attribute("aria-pressed", "true")
        activate(current)
    slider.press("Home")
    for _ in range(int(initial) - int(slider.get_attribute("min"))):
        slider.press("ArrowRight")
    expect(slider).to_have_value(initial)

    # Refresh only the empty-name port to keep other live observations visible mid-query.
    activate(blank)
    switch.release_read.clear()
    try:
        open_port_controls(page)
        page.locator("#refreshPorts").click()
        outer = page.locator('.matrix-tile[data-key$="|13"][data-phase="loading"]')
        expect(outer).to_be_visible(timeout=15000)
        pending = page.locator(".matrix-tiles").filter(has=outer)
        outer_size = grid_geometry(pending.locator("button"), columns=columns, touch=mobile)
        check_name(pending.locator('[data-key$="|7"]'), "tile-ap-name", LONG_NAME, truncated=True)
        check_name(pending.locator('[data-key$="|8"]'), "tile-neighbor-name", "i-WW")
        expect(pending.locator('[data-key$="|10"] .tile-neighbor-count')).to_have_text("이웃 2대")
        expect(
            outer.locator(".tile-ap-name,.tile-neighbor-name,.tile-neighbor-count")
        ).to_have_count(0)
        expect(outer.locator(".tile-link")).to_have_text("—")
        if shots:
            pending.screenshot(path=shots / f"uniform-grid-in-progress-{width}.png")
        print(
            f"In progress {width}px: {columns} columns, "
            f"{outer_size['width']:.2f} x {outer_size['height']:.2f}px"
        )
    finally:
        switch.release_read.set()
    expect(blank).to_be_visible(timeout=60000)
    page.evaluate("() => api('/api/shutdown', {})")


if __name__ == "__main__":
    mobile = "--webkit" in sys.argv
    run_browser_case(
        exercise,
        switch_factory=GridSwitch,
        second_switch_factory=stacking_switch,
        browser_engine="webkit" if mobile else "chromium",
        touch=mobile,
    )
    print("PASS uniform port tiles, responsive sizing, LLDP labels and full details.")
