"""Draw the app icon (assets/icon.png + assets/icon.ico). Needs Pillow."""
from pathlib import Path

from PIL import Image, ImageDraw

ROOT = Path(__file__).resolve().parent.parent
S = 1024  # draw large, downsample for crisp edges
BLUE, WHITE = (10, 102, 217, 255), (255, 255, 255, 255)

img = Image.new("RGBA", (S, S), (0, 0, 0, 0))
d = ImageDraw.Draw(img)
d.rounded_rectangle((40, 40, S - 40, S - 40), radius=220, fill=BLUE)

w = 78  # stroke width
cx = S // 2
# arrow shaft and head
d.rounded_rectangle((cx - w // 2, 230, cx + w // 2, 600), radius=w // 2, fill=WHITE)
for sign in (-1, 1):
    d.line([(cx + sign * 190, 430), (cx, 620)], fill=WHITE, width=w)
    d.ellipse((cx + sign * 190 - w // 2, 430 - w // 2, cx + sign * 190 + w // 2, 430 + w // 2),
              fill=WHITE)
d.ellipse((cx - w // 2, 620 - w // 2 - 4, cx + w // 2, 620 + w // 2 - 4), fill=WHITE)
# tray
d.line([(270, 640), (270, 770), (S - 270, 770), (S - 270, 640)], fill=WHITE, width=w, joint="curve")
for x, y in ((270, 640), (S - 270, 640), (270, 770), (S - 270, 770)):
    d.ellipse((x - w // 2, y - w // 2, x + w // 2, y + w // 2), fill=WHITE)

out = ROOT / "assets"
out.mkdir(exist_ok=True)
img.resize((256, 256), Image.LANCZOS).save(out / "icon.png")
img.save(out / "icon.ico", sizes=[(16, 16), (24, 24), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
print("wrote", out / "icon.png", out / "icon.ico")
