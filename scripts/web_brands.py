"""Writes the brand marks from Simple Icons, so connectors show each platform's mark without an icon package at run time:
web/brands.js for the web client, and for the native app an asset catalog folder (native/Assets.xcassets/Brands) plus
native/Sources/Brands.swift with each brand's colour.

    npm pack simple-icons && tar -xzf simple-icons-*.tgz
    python3 -I scripts/web_brands.py package
    python3 scripts/generate_xcode.py   # when Brands.swift is new

Simple Icons' drawings are CC0. The marks themselves are trademarks of their owners and are used here only to
identify the platform a connector talks to; see docs/SOURCES.md. A few brands Simple Icons does not carry are drawn from the
mark on the brand's own site (OWN); the rest keep a monogram.
"""
import json
import re
import sys
from pathlib import Path

# connector kind -> Simple Icons slug
BRANDS = {"homeassistant": "homeassistant", "smartthings": "smartthings", "openhab": "openhab", "hue": "philipshue",
          "dirigera": "ikea", "lgthinq": "lg", "lifx": "lifx", "shelly": "shelly",
          "apple": "apple", "fitness": "apple", "google": "google",   # health connectors
          "gmail": "gmail", "icloud": "icloud", "qq": "qq"}           # mail
# Brands Simple Icons does not carry: a monogram on the brand's own colour.
MONOGRAMS = {"whoop": ("WH", "1A1A1A"), "outlook": ("O", "0078D4"), "163": ("163", "D7000F"), "126": ("126", "1E9E4A"), "imap": ("@", "6B6B6B")}
# Brands Simple Icons does not carry, with the mark from the brand's own site: kind -> (viewBox, path, colour, where it came from).
OWN = {
    "twilio": ("0 4 40 40", "M20 4C16.0443 4 12.1775 5.17301 8.88848 7.37069C5.59946 9.56838 3.03602 12.692 1.5223 16.3466C0.00858271 20.0012 -0.387411 24.0226 0.384395 27.9023C1.1562 31.7819 3.06114 35.3456 5.85832 38.1426C8.65549 40.9396 12.2193 42.8443 16.099 43.6159C19.9787 44.3874 24 43.9912 27.6545 42.4772C31.309 40.9633 34.4325 38.3996 36.63 35.1105C38.8275 31.8213 40.0003 27.9544 40 23.9988C39.9997 18.6946 37.8925 13.6078 34.1418 9.85737C30.3911 6.1069 25.3041 3.99995 20 4ZM20 38.7287C17.087 38.7285 14.2396 37.8645 11.8176 36.246C9.39569 34.6275 7.50806 32.3271 6.39343 29.6359C5.2788 26.9446 4.98723 23.9832 5.55558 21.1263C6.12393 18.2693 7.52668 15.645 9.58646 13.5852C11.6462 11.5254 14.2705 10.1227 17.1275 9.55432C19.9845 8.98597 22.9459 9.27754 25.6371 10.3922C28.3284 11.5068 30.6287 13.3944 32.2472 15.8164C33.8658 18.2383 34.7298 21.0858 34.73 23.9988C34.7301 25.9331 34.3491 27.8486 33.6089 29.6358C32.8687 31.4229 31.7837 33.0468 30.4159 34.4146C29.048 35.7824 27.4242 36.8674 25.637 37.6077C23.8499 38.3479 21.9344 38.7288 20 38.7287Z M20.8223 19.0186C20.8223 18.1961 21.0662 17.392 21.5231 16.7081C21.9801 16.0242 22.6296 15.4912 23.3895 15.1764C24.1494 14.8617 24.9856 14.7793 25.7923 14.9398C26.5991 15.1002 27.3401 15.4963 27.9217 16.0779C28.5033 16.6595 28.8994 17.4006 29.0599 18.2073C29.2203 19.014 29.138 19.8502 28.8232 20.6101C28.5084 21.37 27.9754 22.0195 27.2915 22.4765C26.6076 22.9335 25.8035 23.1774 24.981 23.1774C24.4349 23.1774 23.8941 23.0698 23.3895 22.8608C22.885 22.6518 22.4265 22.3455 22.0403 21.9593C21.6541 21.5731 21.3478 21.1147 21.1388 20.6101C20.9298 20.1055 20.8223 19.5647 20.8223 19.0186Z M20.8223 28.9808C20.822 28.1582 21.0657 27.3541 21.5225 26.67C21.9794 25.9859 22.6288 25.4527 23.3887 25.1377C24.1486 24.8228 24.9848 24.7402 25.7916 24.9006C26.5984 25.0609 27.3395 25.4569 27.9213 26.0384C28.503 26.62 28.8992 27.361 29.0598 28.1678C29.2203 28.9745 29.138 29.8108 28.8233 30.5708C28.5086 31.3307 27.9756 31.9803 27.2916 32.4373C26.6077 32.8944 25.8036 33.1383 24.981 33.1383C23.8783 33.1383 22.8206 32.7004 22.0408 31.9207C21.2609 31.141 20.8226 30.0836 20.8223 28.9808Z M10.8608 28.9808C10.8606 28.1582 11.1043 27.3541 11.5611 26.67C12.0179 25.9859 12.6673 25.4527 13.4272 25.1377C14.1871 24.8228 15.0234 24.7402 15.8302 24.9006C16.637 25.0609 17.3781 25.4569 17.9598 26.0384C18.5416 26.62 18.9378 27.361 19.0983 28.1678C19.2589 28.9745 19.1766 29.8108 18.8619 30.5708C18.5472 31.3307 18.0141 31.9803 17.3302 32.4373C16.6463 32.8944 15.8422 33.1383 15.0196 33.1383C13.9168 33.1383 12.8592 32.7004 12.0793 31.9207C11.2994 31.141 10.8611 30.0836 10.8608 28.9808Z M10.8608 19.0186C10.8608 18.1961 11.1047 17.392 11.5617 16.7081C12.0187 16.0242 12.6682 15.4912 13.4281 15.1764C14.188 14.8617 15.0242 14.7793 15.8309 14.9398C16.6376 15.1002 17.3787 15.4963 17.9603 16.0779C18.5419 16.6595 18.938 17.4006 19.0984 18.2073C19.2589 19.014 19.1765 19.8502 18.8618 20.6101C18.547 21.37 18.014 22.0195 17.3301 22.4765C16.6462 22.9335 15.8421 23.1774 15.0196 23.1774C14.4735 23.1774 13.9327 23.0698 13.4281 22.8608C12.9235 22.6518 12.4651 22.3455 12.0789 21.9593C11.6927 21.5731 11.3864 21.1147 11.1774 20.6101C10.9684 20.1055 10.8608 19.5647 10.8608 19.0186Z", "F22F46",
               "the logo in the page header of https://www.twilio.com/en-us/press (the circle and four dots, without the wordmark)"),
    "telnyx": ("0 0 72 72", "M62.8955 52.5465C63.9678 50.6227 64.273 48.3736 63.7513 46.2396C63.6018 45.6498 63.3951 45.0755 63.134 44.5246C63.0515 44.3439 62.9578 44.1684 62.8534 43.9991L54.3508 28.1628H45.6519L54.5473 44.7598C54.8266 45.2421 54.9734 45.788 54.9734 46.3434C54.9734 46.8988 54.8266 47.4446 54.5473 47.927C54.2752 48.3777 53.8891 48.751 53.4267 49.0104C52.9643 49.2698 52.4415 49.4064 51.9095 49.4069H41.7654C41.6531 50.952 41.1664 52.448 40.3461 53.7692C39.5258 55.0905 38.3958 56.1984 37.0511 57H55.6978C56.9426 56.9184 58.1543 56.569 59.2475 55.9765C60.7904 55.2273 62.0635 54.0303 62.8955 52.5465Z M15.2053 17.8037H23.9042L28.2397 9.85101C28.6065 9.16466 29.1564 8.59008 29.8302 8.18926C30.504 7.78844 31.2761 7.5766 32.063 7.5766C32.85 7.5766 33.622 7.78844 34.2958 8.18926C34.9696 8.59008 35.5196 9.16466 35.8864 9.85101L40.0955 17.8037H48.7945L42.6211 6.2965C41.5975 4.39527 40.068 2.80466 38.1968 1.69537C36.3257 0.586089 34.1834 0 31.9999 0C29.8164 0 27.6741 0.586089 25.8029 1.69537C23.9318 2.80466 22.4023 4.39527 21.3787 6.2965L15.2053 17.8037Z M22.2207 26.7799C22.3119 25.2411 22.7794 23.7469 23.5832 22.4247C24.3869 21.1026 25.5032 19.9916 26.8368 19.1868H10.0001V26.7799H22.2207Z M23.6093 27.3331V48.0792H27.5659C28.3911 48.0701 29.1905 47.7947 29.8417 47.2951C30.493 46.7955 30.9602 46.0993 31.1718 45.3131C31.2569 45.0114 31.2994 44.6996 31.2981 44.3864V31.9387C31.3017 30.5874 31.846 29.292 32.8127 28.3339C33.7794 27.3758 35.0905 26.8323 36.4613 26.8213H54.0697V19.1868H31.8593C29.67 19.1904 27.5716 20.0503 26.0248 21.5777C24.478 23.105 23.6093 25.1749 23.6093 27.3331Z M0.865818 44.5248C0.604767 45.0756 0.398034 45.65 0.248471 46.2398C-0.272743 48.3783 0.0323602 50.6315 1.10434 52.5605C1.93859 54.0392 3.21144 55.2311 4.75229 55.9766C5.84555 56.5692 7.05718 56.9185 8.30203 57.0001H31.9997C34.2324 57.0001 36.3736 56.1258 37.9524 54.5696C39.5311 53.0133 40.4181 50.9026 40.4181 48.7017V28.1492H36.4053C35.4188 28.1743 34.4809 28.5764 33.7897 29.2707C33.0985 29.965 32.7082 30.8969 32.7012 31.8696V44.3173C32.6975 45.6685 32.1504 46.963 31.1799 47.9171C30.2094 48.8713 28.8946 49.407 27.524 49.407H12.1604C11.6284 49.4066 11.1057 49.27 10.6433 49.0105C10.1809 48.7511 9.79478 48.3778 9.5227 47.9271C9.24352 47.4422 9.09676 46.8942 9.09676 46.3366C9.09676 45.779 9.24352 45.231 9.5227 44.7461L18.4181 28.1492H9.64897L1.14643 43.9854C1.04822 44.179 0.950001 44.345 0.865818 44.5248Z", "00E3AA",
               "https://telnyx.com/images/homepage-september-2026/models/telnyx.svg; colour from https://telnyx.com/media-kit"),
}
# Marks that are black in Simple Icons read better in the text colour.
INK = {"apple", "fitness"}

TEMPLATE = '''// Brand marks from Simple Icons %(version)s (https://simpleicons.org, drawings CC0). Generated by scripts/web_brands.py.
// The marks are trademarks of their owners, shown only to identify the platform a connector talks to.
const NS = 'http://www.w3.org/2000/svg';

const MARKS = %(marks)s;
// Marks drawn on their own area rather than Simple Icons' 24 by 24.
const AREAS = %(areas)s;

const COLORS = %(colors)s;
const MONOGRAMS = %(monograms)s;

/** The brand's colour as #rrggbb, or null when its mark is drawn in the text colour. */
export function brandColor(kind) { return COLORS[kind] ? '#' + COLORS[kind] : (MONOGRAMS[kind] ? '#' + MONOGRAMS[kind][1] : null); }
/** The letters standing in for a brand Simple Icons does not carry. */
export function brandLetters(kind) { return MONOGRAMS[kind] ? MONOGRAMS[kind][0] : null; }

/** The mark for a connector kind as an SVG in the text colour, or null when there is none. */
export function brandLogo(kind, size = 20) {
  const d = MARKS[kind];
  if (!d) return null;
  const svg = document.createElementNS(NS, 'svg');
  svg.setAttribute('viewBox', AREAS[kind] || '0 0 24 24');
  svg.setAttribute('width', String(size));
  svg.setAttribute('height', String(size));
  svg.setAttribute('fill', 'currentColor');
  svg.setAttribute('aria-hidden', 'true');
  const path = document.createElementNS(NS, 'path');
  path.setAttribute('d', d);
  svg.append(path);
  return svg;
}
'''


SWIFT = """// Generated by scripts/web_brands.py from Simple Icons %(version)s. Do not edit.
import SwiftUI

/// Brand marks for connectors: an image in Assets.xcassets/Brands, or letters, each in the brand's colour on a faint wash of it.
enum Brands {
    /// Kinds with a mark in the asset catalog ("Brand-<kind>").
    static let marks: Set<String> = [%(marks)s]
    /// Brand colours (hex); a kind not here is drawn in the text colour.
    static let colors: [String: UInt32] = [%(colors)s]
    static let letters: [String: String] = [%(letters)s]
}
"""


def native(package, marks, colors, version, areas):
    root = Path(__file__).resolve().parent.parent / "native"
    folder = root / "Assets.xcassets" / "Brands"
    folder.mkdir(parents=True, exist_ok=True)
    (folder / "Contents.json").write_text(json.dumps({"info": {"author": "xcode", "version": 1}, "properties": {"provides-namespace": False}}, indent=2) + "\n")
    for kind, d in marks.items():
        item = folder / f"Brand-{kind}.imageset"
        item.mkdir(exist_ok=True)
        (item / f"{kind}.svg").write_text(f'<svg xmlns="http://www.w3.org/2000/svg" width="24" height="24" viewBox="{areas.get(kind, "0 0 24 24")}"><path d="{d}"/></svg>\n')
        (item / "Contents.json").write_text(json.dumps({"images": [{"filename": f"{kind}.svg", "idiom": "universal"}], "info": {"author": "xcode", "version": 1},
                                                         "properties": {"preserves-vector-representation": True, "template-rendering-intent": "template"}}, indent=2) + "\n")
    swift = SWIFT % {"version": version, "marks": ", ".join(json.dumps(k) for k in marks),
                     "colors": ", ".join(f"{json.dumps(k)}: 0x{v}" for k, v in {**colors, **{k: c for k, (_, c) in MONOGRAMS.items()}}.items()),
                     "letters": ", ".join(f"{json.dumps(k)}: {json.dumps(t)}" for k, (t, _) in MONOGRAMS.items())}
    (root / "Sources" / "Brands.swift").write_text(swift)


def main():
    package = Path(sys.argv[1])
    marks = {}
    for kind, slug in BRANDS.items():
        found = re.search(r'<path d="([^"]+)"', (package / "icons" / f"{slug}.svg").read_text())
        if not found:
            sys.exit(f"no path in {slug}.svg")
        marks[kind] = found.group(1)
    data = json.loads((package / "data" / "simple-icons.json").read_text())
    data = data if isinstance(data, list) else data["icons"]
    hexes = {i["slug"]: i["hex"] for i in data if i.get("slug")}
    colors = {kind: hexes[slug] for kind, slug in BRANDS.items() if kind not in INK and hexes.get(slug, "000000") != "000000"}
    version = json.loads((package / "package.json").read_text())["version"]
    for kind, (area, d, color, _) in OWN.items():
        marks[kind], colors[kind] = d, color
    areas = {kind: area for kind, (area, *_) in OWN.items()}
    lines = ",\n".join(f"  {json.dumps(k)}: {json.dumps(v)}" for k, v in marks.items())
    target = Path(__file__).resolve().parent.parent / "web" / "brands.js"
    target.write_text(TEMPLATE % {"version": version, "marks": "{\n" + lines + ",\n}", "areas": json.dumps(areas), "colors": json.dumps(colors),
                                  "monograms": json.dumps({k: list(v) for k, v in MONOGRAMS.items()}, ensure_ascii=False)})
    native(package, marks, colors, version, areas)
    print(f"wrote {target} with {len(marks)} marks")


if __name__ == "__main__":
    main()
