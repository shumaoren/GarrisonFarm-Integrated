"""Read the battle UI for promotion farming."""

import json
from pathlib import Path
import re
import unicodedata


ROUND_BOX = (728, 32, 778, 68)
FIELD_STATUS_BOX = (1644, 562, 1919, 612)
FIELD_DIGIT_BOX = (1878, 575, 1902, 599)
FIELD_DIGIT_WHITE_MIN = 175
NAME_RETRY_DELAY = 1.5


class BattleScreen:
    def __init__(self, game):
        self.game = game
        self.original_shop_cards = game['shop_cards']
        self.original_slots_left = game['slots_left']
        self.field_digit_templates = {}
        path = Path(game['HERE']) / 'field_slot_digits.json'
        if path.is_file():
            templates = json.loads(path.read_text(encoding='utf-8'))
            self.field_digit_templates = {
                int(digit): game['np'].array([[pixel == '#' for pixel in row] for row in sample['rows']])
                for digit, sample in templates.items()
            }

    def shop_cards(self, screen):
        if screen.shape[:2] != (1080, 1920):
            return self.original_shop_cards(screen)
        image = screen.copy()
        for x in self.game['CARDS']:
            # The profession icon sits to the left of the name on each card.
            image[1006:1044, x - 113:x - 70] = 28
        return self.original_shop_cards(image)

    def field_slots(self, screen):
        slots = self.field_slots_from_template(screen)
        if slots is not None:
            return slots
        slots = self.original_slots_left(screen)
        if slots is not None and 0 <= slots <= 8:
            return slots
        return self.field_slots_from_label(screen)

    def field_slots_from_template(self, screen, *, y_offset=0):
        if len(self.field_digit_templates) < 2 or screen.shape[:2] != (1080, 1920):
            return None
        # Both dots of the colon should be present before testing the digit.
        colon = screen[578 + y_offset:598 + y_offset, 1856:1870].min(axis=2) >= FIELD_DIGIT_WHITE_MIN
        if not ((colon[:7].sum(axis=0) >= 2) & (colon[-8:].sum(axis=0) >= 2)).any():
            return None
        x1, y1, x2, y2 = FIELD_DIGIT_BOX
        mask = screen[y1 + y_offset:y2 + y_offset, x1:x2].min(axis=2) >= FIELD_DIGIT_WHITE_MIN
        scores = sorted(((mask & template).sum() / (mask | template).sum(), digit)
                        for digit, template in self.field_digit_templates.items())
        best, digit = scores[-1]
        if best >= 0.88 and best - scores[-2][0] >= 0.12:
            return digit
        return None

    def field_slots_from_label(self, screen, *, y_offset=0):
        x1, y1, x2, y2 = FIELD_STATUS_BOX
        box = (x1, y1 + y_offset, x2, y2 + y_offset)
        text = ''.join(str(t) for _, _, t in self.game['ocr'](screen, box))
        label = ''.join(unicodedata.normalize('NFKC', text).upper().split())
        match = re.search(r'剩余可放置角色数?:?([0-8O])(?![\dA-Z])', label)
        return int(match[1].replace('O', '0')) if match else None
