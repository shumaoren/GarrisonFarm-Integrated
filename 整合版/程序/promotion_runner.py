"""Bridge the level-one policy to the bundled GarrisonFarm controls."""

from collections import Counter
from dataclasses import replace
import json
import math
import os
from pathlib import Path
import re
import time

from battle_screen import BattleScreen, NAME_RETRY_DELAY, ROUND_BOX
from level1_promotion import (
    COIN_COST, COIN_NAME, DEPLOYMENT_BLACKLIST, FINAL_EXIT_REASON, LAST_ROUND, REFRESH_COST, UNIT_COST,
    Offer, Snapshot, Unit, decide, normalized_name, settled_promotions,
)


HP_BOX = (1188, 32, 1255, 69)
SHOP_LEVEL_BOX = (521, 870, 632, 946)
FREEZE_BOX = (1450, 646, 1674, 728)
SHOP_TOGGLE_BOX = (1680, 967, 1900, 1054)
SHOP_TOGGLE_POINT = (1782, 1005)
COLLAPSED_FIELD_STATUS_OFFSET = 114
REFRESH_BADGE_BOX = (1722, 626, 1865, 672)
FROZEN_LABELS = ('解冻', '取消冻结', '已冻结')
LOBBY_BUTTONS_BOX = (1130, 905, 1919, 1070)
EXIT_DIALOG_BOX = (380, 200, 1550, 900)
HOME_BUTTON_BOX = (1050, 770, 1919, 1079)
RESULT_ROUND_BOX = (72, 447, 305, 568)
EXIT_SCREEN_TIMEOUT = 8.0
EXIT_LOBBY_DELAY = 8.0
EXIT_LOBBY_TIMEOUT = 12.0
EXIT_SETTLEMENT_TIMEOUT = 30
BENCH_POINTS = tuple((1381 - 121 * index, 678) for index in range(10))
# Rightmost equipment slot with the shop open at 1920x1080.
COIN_PICKUP_POINT = (1254, 605)
BENCH_BADGE_OFFSET = 43
CANCEL_DRAG_POINT = (375, 126)
DEPLOY_LIFT_POINT = (760, 195)
# Stationary parts of the board that shift when the shop or camera moves.
DEPLOY_VIEW_ANCHORS = ((885, 448, 945, 478), (758, 288, 800, 320), (980, 494, 1020, 537))
INNER_TILE_OFFSETS = ((18, 0), (-18, 0), (0, 18), (0, -18),
                      (26, 0), (-26, 0), (0, 26), (0, -26),
                      (22, 22), (-22, 22), (22, -22), (-22, -22),
                      (26, 26), (-26, 26), (26, -26), (-26, -26))
PATH1_SIDE_OFFSETS = ((42, 0), (-42, 0), (42, 8), (-42, 8))
DEPLOY_ATTEMPTS = 3
DRAG_PIXELS_PER_STEP = 56
MIN_SDK_DRAG_TIME = 0.16
OCR_NAME_ALIASES = {'N复栎': '夏栎'}


class OperatorNameError(RuntimeError):
    """The shop detected an operator card but could not identify its name."""


def rank_badge_outline(np):
    outline = np.zeros((45, 45), dtype=np.uint8)
    # Grayscale profiles across each edge of a badge at 1920x1080. The mask
    # leaves out the actual rank numeral and any texture outside the corners.
    outline[2:7, 11:33] = np.array((100, 84, 22, 8, 9))[:, None]
    outline[38:43, 11:33] = np.array((13, 12, 12, 47, 99))[:, None]
    outline[10:34, 2:7] = (92, 55, 15, 11, 10)
    outline[10:34, 38:43] = (10, 17, 39, 105, 103)
    return outline, (outline > 0).astype(np.uint8)


def recognized_name(text):
    name = normalized_name(text)
    return OCR_NAME_ALIASES.get(name, name)


def cards_from_screen(cards, roster):
    offers = []
    rewards = []
    for card in cards:
        if card['empty']:
            continue
        key = recognized_name(card['name'])
        cost = card['cost']
        if key == COIN_NAME and cost == COIN_COST and not card['op']:
            offers.append(Offer(card['i'], key, 1, cost))
            continue
        known = roster.get(key)
        if known:
            tier, profession = known
            if (tier, cost) == (1, 2):
                offers.append(Offer(card['i'], key, 1, 2, profession))
            elif (tier, cost) == (2, 0):
                rewards.append(Offer(card['i'], key, 2, 0, profession))
            elif tier <= 2:
                raise RuntimeError(f'未能核对 {key} 的招募费用（读到 {cost}）')
        elif card['op'] and cost in (0, 2):
            raise OperatorNameError(f'有干员牌的名称未识别（第 {card["i"] + 1} 格：{card["name"]}）')
    return tuple(offers), tuple(rewards)


class PromotionRun:
    def __init__(self, game):
        self.game = game
        self.directory = Path(game['HERE'])
        self.progress = self.directory / 'promotion_progress.json'
        self.roster = json.loads((self.directory / 'promotion_roster.json').read_text(encoding='utf-8-sig'))
        self.screen = BattleScreen(game)
        self.target = int(os.environ.get('GF_PROMOTION_TARGET') or '200')
        if self.target < 1:
            raise ValueError('晋升目标须大于 0')
        self.total = 0
        if self.progress.exists():
            self.total = json.loads(self.progress.read_text(encoding='utf-8'))['settled_promotions']
            if type(self.total) is not int or self.total < 0:
                raise ValueError('保存的晋升进度无效')
        self.original_log = game['log']
        self.original_pick = game['pick_strategy']
        self.rank_badge, self.rank_badge_mask = rank_badge_outline(game['np'])
        self.reset()

    def reset(self):
        self.units = {}
        self.bench = {}
        self.rounds = Counter()
        self.next_id = 0
        self.round_number = 0
        self.coin_held = False
        self.rejected_drop_points = {}
        self.confirmed_drop_points = {}
        self.abandon_requested = False
        self.exited_round = None
        self.strategy_confirmed = False

    def log(self, *parts):
        message = str(parts[0]) if parts else ''
        if '断线重连' in message:
            raise SystemExit('[刷晋升] 运行中回到断线重连界面，请重新启动刷晋升以放弃旧局。')
        self.original_log(*parts)
        if not message.startswith(('本局通关，策略 ', '本局失败，策略 ')):
            return
        if not self.round_number and self.exited_round is None:
            return
        completed = self.round_number if self.exited_round is None else self.exited_round
        count = settled_promotions(self.rounds, completed)
        total = self.total + count
        if count:
            temp = self.progress.with_suffix('.tmp')
            temp.write_text(json.dumps({'settled_promotions': total}) + '\n', encoding='utf-8')
            temp.replace(self.progress)
        self.total = total
        self.original_log(f'[刷晋升] 结算至第 {completed} 轮，本局确认 {count} 次，脚本累计 {total}/{self.target} 次')
        self.reset()
        if total >= self.target:
            raise SystemExit('[刷晋升] 已达到脚本确认目标，停在结算画面。请核对奖章实际进度。')

    def pick_strategy(self, done):
        previous = self.game['passed_count']

        def select_goliath(panel):
            text = ''.join(str(t) for _, _, t in panel).replace(' ', '')
            return 0 if self.game['strategy_name'](panel) == '歌利亚' or '歌利亚' in text else self.game['TARGET']

        self.game['passed_count'] = select_goliath
        try:
            found = self.original_pick(done)
        finally:
            self.game['passed_count'] = previous
        self.strategy_confirmed = found is not None
        return found

    def pick_squad(self):
        action = decide(Snapshot(round_number=0, funds=0, hp=0, bench_free=0, phase='squad_setup'))
        if action.kind == 'confirm_squad':
            self.original_log(f'[刷晋升] {action.reason}')
            self.game['tap'](1686, 986, 2)

    def text(self, screen, box):
        return ''.join(str(t) for _, _, t in self.game['ocr'](screen, box)).replace(' ', '')

    def read_shop(self, screen):
        try:
            offers, rewards = cards_from_screen(self.screen.shop_cards(screen), self.roster)
        except OperatorNameError:
            time.sleep(NAME_RETRY_DELAY)
            screen = self.game['shot']()
            offers, rewards = cards_from_screen(self.screen.shop_cards(screen), self.roster)
        return screen, offers, rewards

    def free_refreshes(self, screen):
        label = normalized_name(self.text(screen, REFRESH_BADGE_BOX)).upper()
        match = re.fullmatch(r'FREE[X×*](\d+)', label)
        if match:
            return int(match[1])
        if label == str(REFRESH_COST):
            return 0
        return None

    def refresh_shop(self, funds, previous_offers):
        game = self.game
        before = game['shot']()
        game['tap'](*game['REFRESH'], 0.8)
        for attempt in range(2):
            screen, offers, rewards = self.read_shop(game['shot']())
            balance = game['read_gold'](screen)
            if balance == funds - REFRESH_COST:
                return offers, rewards, balance
            if attempt == 0:
                free_before = self.free_refreshes(before)
            if balance != funds or not free_before:
                break
            free_after = self.free_refreshes(screen)
            if free_after == free_before - 1 or (free_after is None and offers != previous_offers):
                self.original_log(f'[刷晋升] R{self.round_number} 免费刷新已确认，资金仍为 {funds}')
                return offers, rewards, balance
            if attempt == 0:
                time.sleep(NAME_RETRY_DELAY)
                continue
            raise RuntimeError(f'免费刷新结果未确认，刷新前 {free_before} 次，刷新后读到 {free_after}')
        raise RuntimeError(f'刷新结果未确认，原有 {funds} 资金，读到 {balance}，免费次数读到 {free_before}')

    def lobby_buttons(self, screen):
        buttons = {}
        for x, y, item in self.game['ocr'](screen, LOBBY_BUTTONS_BOX):
            label = ''.join(str(item).split())
            if '断线重连' in label and x >= 1460:
                buttons['reconnect'] = (x, y)
            elif label == '放弃模拟' and 1140 <= x <= 1445:
                buttons['abandon'] = (x, y)
            elif '极速模拟' in label:
                buttons['start'] = (x, y)
        return buttons

    def exit_confirmation(self, screen, *, abandon):
        dialog = [(x, y, ''.join(str(label).split()))
                  for x, y, label in self.game['ocr'](screen, EXIT_DIALOG_BOX)]
        dialog_text = ''.join(label for _, _, label in dialog)
        if abandon:
            matches = '确认退出模拟' in dialog_text and '结算' in dialog_text
        else:
            matches = '暂时离开模拟' in dialog_text and '记录将保留' in dialog_text
        if not matches:
            return None
        return next(((x, y) for x, y, label in dialog
                     if x >= 960 and 650 <= y <= 800 and label == '确认'), None)

    def wait_for_screen(self, recognize, error, *, timeout=EXIT_SCREEN_TIMEOUT):
        deadline = time.monotonic() + timeout
        while True:
            found = recognize(self.game['shot']())
            if found is not None:
                return found
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(0.45, remaining))
        raise RuntimeError(error)

    def abandon_match(self, button, *, confirm=None):
        if confirm is None:
            self.original_log('[刷晋升] 大厅有未完成的对局，点击左侧「放弃模拟」。')
            self.game['tap'](*button, 0.8)
            confirm = self.wait_for_screen(
                lambda screen: self.exit_confirmation(screen, abandon=True),
                '未识别放弃模拟的结算确认框',
            )
        self.abandon_requested = True
        self.game['tap'](*confirm, 1.1)

    def return_home_button(self, screen):
        hit = self.game['find'](self.game['ocr'](screen, HOME_BUTTON_BOX), '返回主页')
        return hit[:2] if hit else None

    def abandon_interrupted_at_start(self):
        game = self.game
        buttons = {}
        for _ in range(3):
            buttons = self.lobby_buttons(game['shot']())
            if 'reconnect' in buttons and 'abandon' in buttons:
                break
            if 'start' in buttons and 'reconnect' not in buttons:
                return
            time.sleep(0.35)
        if 'reconnect' not in buttons:
            return
        if 'abandon' not in buttons:
            raise SystemExit('[刷晋升] 看到断线重连，但未能识别左侧的「放弃模拟」。')

        try:
            self.abandon_match(buttons['abandon'])
        except RuntimeError as exc:
            raise SystemExit(f'[刷晋升] {exc}') from exc
        returned_home = False
        for _ in range(10):
            image = game['shot']()
            buttons = self.lobby_buttons(image)
            if 'start' in buttons and 'reconnect' not in buttons:
                self.reset()
                self.original_log('[刷晋升] 旧局已结束，准备开始新的极速模拟。')
                return
            if not returned_home:
                hit = self.return_home_button(image)
                if hit:
                    game['tap'](*hit, 1.0)
                    returned_home = True
                    continue
            time.sleep(0.45)
        raise SystemExit('[刷晋升] 已点击「放弃模拟」，但未确认回到可开新局的大厅。')

    def shop_level(self, screen):
        # The bundled reader reports the upgrade price, not the current level.
        panel = self.text(screen, SHOP_LEVEL_BOX).upper().replace('１', '1')
        if panel in ('I', '1', 'L', 'Ⅰ', '|'):
            return 1
        return None

    def expected_field_slots(self):
        return 8 - sum(unit.tile is not None for unit in self.units.values())

    def field_check(self, screen, *, after_field_sale=False):
        slots = self.screen.field_slots(screen)
        expected = self.expected_field_slots()
        for _ in range(2):
            if slots is not None:
                break
            time.sleep(0.25)
            screen = self.game['shot']()
            slots = self.screen.field_slots(screen)
        if slots != expected and after_field_sale and '收起' in self.text(screen, SHOP_TOGGLE_BOX):
            self.original_log(f'[刷晋升] 场上售卖后剩余部署位读到 {slots}，预计为 {expected}，收起再展开商店复核')
            self.switch_shop_view('展开')
            screen = self.switch_shop_view('收起')
            slots = self.screen.field_slots(screen)
            if slots == expected:
                self.original_log(f'[刷晋升] 切换商店后已确认剩余部署位 {slots}')
        if slots != expected:
            raise RuntimeError(f'场上人数未核对，预计剩 {expected} 个部署位，读到 {slots}')
        return screen

    def register(self, card, *, point=None, tile=None, elite=False):
        self.next_id += 1
        identifier = str(self.next_id)
        self.units[identifier] = Unit(identifier, card.name, card.tier, self.roster[card.name][1], elite, tile)
        if point is not None:
            self.bench[identifier] = point

    def bench_slots(self, screen):
        cv2 = self.game['cv2']
        strip = cv2.cvtColor(screen[573:613], cv2.COLOR_BGR2GRAY)
        occupied = set()
        for point in BENCH_POINTS:
            # The tier badge sits above and to the left of each sprite.
            x = point[0] - BENCH_BADGE_OFFSET
            rim = strip[:3, x - 9:x + 9].mean()
            fill = strip[5:9, x - 9:x + 9].mean()
            interior = strip[8:39, x - 12:x + 12]
            if rim - fill >= 45 and (interior < 110).mean() >= 0.62:
                occupied.add(point)
        return occupied

    def resting_tile_point(self, tile):
        position = self.game['LAYOUT'].get('prep', {}).get(tile)
        if position is not None:
            return tuple(position)
        # Older tile layouts only store coordinates for the zoomed-in drag view.
        x, y = self.game['tile_xy'](tile)
        return round(0.85 * x + 98), round(0.85 * y - 10)

    def pickup_point(self, unit):
        if unit.tile is None:
            return self.bench[unit.id]
        x, y = self.resting_tile_point(unit.tile)
        return x, y - 24

    def field_scan_order(self, tile):
        x, y = self.game['tile_xy'](tile)
        return y, -x

    def purchase(self, card, funds):
        occupied = set(self.bench.values())
        if len(occupied) == len(BENCH_POINTS):
            raise RuntimeError('整备区已满，无法招募')
        copies = [unit for unit in self.units.values() if not unit.elite and unit.key == card.key]
        promoted = len(copies) == 2
        consumed = copies if promoted else ()
        field_tiles = {unit.tile for unit in consumed if unit.tile is not None}
        tile = min(field_tiles, key=self.field_scan_order, default=None)
        point = None
        if tile is None:
            occupied.difference_update(self.bench[unit.id] for unit in consumed)
            point = next(point for point in BENCH_POINTS if point not in occupied)

        if not self.game['buy'](card.slot, card.cost):
            raise RuntimeError(f'游戏未确认招募 {card.name}')
        after, offers, rewards = self.read_shop(self.game['shot']())
        balance = self.game['read_gold'](after)
        if card.cost and balance != funds - card.cost:
            raise RuntimeError(f'招募 {card.name} 后的资金变化未确认')
        if promoted and not rewards:
            raise RuntimeError(f'招募 {card.name} 后未确认晋升奖励')
        if not card.cost and not promoted and rewards:
            raise RuntimeError(f'领取 {card.name} 后仍显示免费招募')
        for old in consumed:
            self.units.pop(old.id)
            self.bench.pop(old.id, None)
        self.register(card, point=point, tile=tile, elite=promoted)
        if promoted:
            self.rounds[self.round_number] += 1
            position = f'场上 {tile}' if tile is not None else f'整备区 {point}'
            self.original_log(f'[刷晋升] 第 {self.round_number} 轮确认 {card.name} 晋升，精锐登记在{position}，'
                              f'本轮 {self.rounds[self.round_number]} 次')
        else:
            self.original_log(f'[刷晋升] R{self.round_number} {card.name} 已登记在整备区 {point}')
        if len(field_tiles) == 2:
            self.field_check(after)
        return offers, rewards, balance

    def buy_coin(self, slot):
        game = self.game
        before, offers, rewards = self.read_shop(game['shot']())
        if rewards or not any(card.slot == slot and card.name == COIN_NAME for card in offers):
            raise RuntimeError('下单前未能确认盟约之币仍在原商店格')
        if game['read_gold'](before) != COIN_COST:
            raise RuntimeError('购买盟约之币前未核对到1资金')
        x = game['CARDS'][slot]
        game['tap'](x, game['CARD_Y'], 0.8)
        confirm_box = (x - 114, 978, x + 114, 1068)
        if '确认购买' not in self.text(game['shot'](), confirm_box):
            raise RuntimeError('未识别到盟约之币的确认购买按钮')
        game['tap'](x, game['CARD_Y'] + 106, 1.2)
        after = game['shot']()
        if game['read_gold'](after) != 0:
            raise RuntimeError('买入盟约之币后未确认扣除1资金')
        self.coin_held = True
        self.original_log(f'[刷晋升] R{self.round_number} 买入盟约之币，下轮从最右装备格取用')

    def redeem_coin(self, screen, target):
        game = self.game
        funds = game['read_gold'](screen)
        if funds is None:
            raise RuntimeError('使用盟约之币前未识别到资金')
        start = COIN_PICKUP_POINT
        lift = DEPLOY_LIFT_POINT
        release = start
        game['touch']('DOWN', *start)
        try:
            self.drag_to(start, lift, 10)
            time.sleep(0.8)
            held = game['shot']()
            name = recognized_name(game['drag_name'](held))
            if name != COIN_NAME:
                time.sleep(NAME_RETRY_DELAY)
                held = game['shot']()
                name = recognized_name(game['drag_name'](held))
                if name != COIN_NAME:
                    raise RuntimeError(f'从装备格 {start} 拖起后应为盟约之币，读到 {name or "空白"}')
            if self.board_view_changed(screen, held):
                dest = tuple(game['tile_xy'](target))
            else:
                dest = self.resting_tile_point(target)
            self.drag_to(lift, dest, 10, placing=True)
            release = dest
            time.sleep(0.25)
        finally:
            if release == start:
                try:
                    game['drag_path'](*lift, *CANCEL_DRAG_POINT, 8)
                finally:
                    game['touch']('UP', *CANCEL_DRAG_POINT)
            else:
                game['touch']('UP', *release)
        time.sleep(0.6)
        after = game['shot']()
        balance = game['read_gold'](after)
        if balance != funds + COIN_COST:
            raise RuntimeError(f'盟约之币使用后未确认到账，原有 {funds} 资金，读到 {balance}')
        self.coin_held = False
        self.original_log(f'[刷晋升] R{self.round_number} 盟约之币到账，资金 {funds} → {balance}')
        if '展开' in self.text(after, SHOP_TOGGLE_BOX):
            game['tap'](*SHOP_TOGGLE_POINT, 0.6)
            after = game['shot']()
        return after

    def deployment_point(self, screen, target):
        game = self.game
        center = tuple(game['tile_xy'](target))
        rejected = self.rejected_drop_points.get(target, ())
        preferred = self.confirmed_drop_points.get(target, center)
        x, y = center
        # A sprite in the preceding row can cover the center of an empty tile.
        offsets = PATH1_SIDE_OFFSETS + INNER_TILE_OFFSETS if target == 'path1' else INNER_TILE_OFFSETS
        points = [preferred, center]
        points.extend((x + dx, y + dy) for dx, dy in offsets)
        for point in dict.fromkeys(points):
            if point in rejected:
                continue
            green = game['is_green'](screen, point) if point == center else game['is_green'](screen, point, 8)
            if green:
                return point
        return None

    def drag_to(self, start, end, min_steps, *, placing=False):
        steps = max(min_steps, math.ceil(math.dist(start, end) / DRAG_PIXELS_PER_STEP))
        controller = self.game.get('_mumu_touch_controller')
        if placing and controller is not None:
            steps = max(steps, math.ceil(MIN_SDK_DRAG_TIME / controller.interval))
        self.game['drag_path'](*start, *end, steps)

    def tile_has_rank_badge(self, screen, tile):
        x, y = self.resting_tile_point(tile)
        return self.rank_badge_at(screen, (x - 45, y - 129), margin=(35, 37))

    def rank_badge_at(self, screen, point, *, margin):
        badge_x, badge_y = point
        dx, dy = margin
        cv2, np = self.game['cv2'], self.game['np']
        roi = screen[badge_y - dy:badge_y + dy + 1, badge_x - dx:badge_x + dx + 1]
        gray = cv2.cvtColor(roi, cv2.COLOR_BGR2GRAY)
        scores = cv2.matchTemplate(gray, self.rank_badge, cv2.TM_CCOEFF_NORMED, mask=self.rank_badge_mask)
        return bool(np.any(np.isfinite(scores) & (scores >= 0.82)))

    def board_view_changed(self, before, after):
        cv2 = self.game['cv2']
        for x1, y1, x2, y2 in DEPLOY_VIEW_ANCHORS:
            delta = cv2.absdiff(before[y1:y2, x1:x2], after[y1:y2, x1:x2])
            if (delta.max(axis=2) >= 36).mean() > 0.10:
                return True
        return False

    def tile_changed_after_deploy(self, before, after, tile):
        if self.board_view_changed(before, after):
            return False
        cv2 = self.game['cv2']
        x, y = self.resting_tile_point(tile)
        region = (slice(y - 56, y + 8), slice(x - 22, x + 22))
        changed = cv2.absdiff(before[region], after[region]).max(axis=2) >= 44
        # A unit on the row below can overlap the lower half of this crop.
        return bool(changed[:32].mean() >= 0.35 and changed[32:].mean() >= 0.35)

    def collapsed_field(self, screen):
        slots = self.screen.field_slots_from_template(screen, y_offset=COLLAPSED_FIELD_STATUS_OFFSET)
        if slots is None:
            slots = self.screen.field_slots_from_label(screen, y_offset=COLLAPSED_FIELD_STATUS_OFFSET)
        badges = (self.game['LAYOUT'] or {}).get('closed_badges', {})
        occupied = {tile for tile, point in badges.items() if self.rank_badge_at(screen, point, margin=(30, 30))}
        return slots, occupied

    def switch_shop_view(self, label):
        self.game['tap'](*SHOP_TOGGLE_POINT, 0.7)
        return self.wait_for_screen(
            lambda screen: screen if label in self.text(screen, SHOP_TOGGLE_BOX) else None,
            f'切换商店后未确认右下角显示「{label}」', timeout=3.0,
        )

    def confirm_deployment_with_shop_closed(self, screen, unit, target, slots_expected):
        game = self.game
        expected = {other.tile for other in self.units.values() if other.tile is not None} | {target}
        if not expected <= game['LAYOUT'].get('closed_badges', {}).keys():
            return False
        if '收起' not in self.text(screen, SHOP_TOGGLE_BOX):
            return False
        closed = self.switch_shop_view('展开')
        try:
            slots, seen = self.collapsed_field(closed)
            if slots == slots_expected and seen == expected:
                self.original_log(f'[刷晋升] 收起商店后确认 {unit.name} 已部署在 {target}')
                return True
            self.original_log(f'[刷晋升] 收起商店后剩余部署位 {slots}，目标场上格 {sorted(expected)}，读到 {sorted(seen)}')
            return False
        finally:
            self.switch_shop_view('收起')

    def deployment_confirmed(self, before, screen, start, unit, target, *, facing_opened):
        before_slots = self.expected_field_slots()
        for attempt in range(3):
            if attempt:
                time.sleep(0.25)
                screen = self.game['shot']()
            slots = self.screen.field_slots(screen)
            still_on_bench = start in self.bench_slots(screen)
            target_occupied = self.tile_has_rank_badge(screen, target) or self.tile_changed_after_deploy(
                before, screen, target,
            )
            if slots == before_slots - 1 and not still_on_bench and target_occupied:
                return True
        if slots == before_slots and still_on_bench and not target_occupied and not facing_opened:
            self.original_log(f'[刷晋升] {unit.name} 落点未生效，部署数未变且整备原位仍有干员')
            return False
        if not still_on_bench and slots in (None, before_slots - 1):
            if self.confirm_deployment_with_shop_closed(screen, unit, target, before_slots - 1):
                return True
        if slots == before_slots - 1 and not still_on_bench:
            raise RuntimeError(f'{unit.name} 已上场，但未在目标格 {target} 确认干员，可能落在其他位置')
        raise RuntimeError(
            f'{unit.name} 部署到 {target} 后可能发生交换或状态未确认，原剩 {before_slots} 个部署位，'
            f'读到 {slots}，整备原位仍有干员={still_on_bench}，目标格有占位迹象={target_occupied}'
        )

    def drag_unit_to_tile(self, unit, start, target):
        game = self.game
        # Holding over a deployable tile can leave the operator there when a drag is interrupted.
        lift = DEPLOY_LIFT_POINT
        game['touch']('DOWN', *start)
        cursor = start
        release_point = None
        try:
            raised = start[0], start[1] - 120
            self.drag_to(start, raised, 6)
            cursor = raised
            self.drag_to(raised, lift, 8)
            cursor = lift
            time.sleep(0.8)
            shot = game['shot']()
            actual_name = recognized_name(game['drag_name'](shot))
            if actual_name != unit.key[1]:
                time.sleep(NAME_RETRY_DELAY)
                shot = game['shot']()
                actual_name = recognized_name(game['drag_name'](shot))
                if actual_name != unit.key[1]:
                    raise RuntimeError(f'拖起的干员应为 {unit.name}，读到 {actual_name or "空白"}，可能已发生交换')
            dest = self.deployment_point(shot, target)
            if dest is None:
                time.sleep(0.25)
                dest = self.deployment_point(game['shot'](), target)
            if dest is not None:
                self.original_log(f'[刷晋升] {unit.name} 尝试落在 {target} {dest}')
                self.drag_to(lift, dest, 10, placing=True)
                release_point = dest
                time.sleep(0.25)
        finally:
            if release_point is not None:
                game['touch']('UP', *release_point)
            else:
                try:
                    game['drag_path'](*cursor, *CANCEL_DRAG_POINT, 8)
                finally:
                    game['touch']('UP', *CANCEL_DRAG_POINT)
        return release_point

    def deploy(self, identifier, target):
        game = self.game
        unit = self.units[identifier]
        if unit.key[1] in DEPLOYMENT_BLACKLIST:
            raise RuntimeError(f'{unit.name} 在部署黑名单中')
        start = self.pickup_point(unit)
        center = tuple(game['tile_xy'](target))
        for attempt in range(DEPLOY_ATTEMPTS):
            before = game['shot']()
            if self.tile_has_rank_badge(before, target):
                raise RuntimeError(f'目标格 {target} 已有干员，与台账不符，避免交换位置')
            dest = self.drag_unit_to_tile(unit, start, target)
            time.sleep(0.6)
            screen = game['shot']()
            if dest is None:
                screen = self.field_check(screen)
                if unit.tile is None and start not in self.bench_slots(screen):
                    raise RuntimeError(f'取消部署后未在整备区确认 {unit.name} 的原位')
                self.original_log(f'[刷晋升] {target} 未找到可用落点')
                return False
            facing_opened = game['in_facing_mode'](screen)
            if facing_opened:
                if not game['set_facing'](*center):
                    raise RuntimeError('部署后朝向选择界面仍未关闭')
                screen = game['shot']()
            if unit.tile is None and not self.deployment_confirmed(
                before, screen, start, unit, target, facing_opened=facing_opened,
            ):
                self.rejected_drop_points.setdefault(target, set()).add(dest)
                self.original_log(f'[刷晋升] {target} 落点 {dest} 已被游戏退回，尝试次数 {attempt + 1}/{DEPLOY_ATTEMPTS}')
                continue
            if unit.tile is None:
                self.bench.pop(identifier)
                self.confirmed_drop_points[target] = dest
            else:
                screen = self.field_check(screen)
                placed = self.tile_has_rank_badge(screen, target) or self.tile_changed_after_deploy(before, screen, target)
                if not placed or self.tile_has_rank_badge(screen, unit.tile):
                    raise RuntimeError(f'{unit.name} 从 {unit.tile} 移到 {target} 的结果未确认')
            self.units[identifier] = replace(unit, tile=target)
            self.original_log(f'[刷晋升] R{self.round_number} {unit.name} 登记在场上 {target}')
            return True
        return False

    def sell(self, identifier):
        unit = self.units[identifier]
        point = self.pickup_point(unit)
        if not self.game['sell_at'](*point):
            raise RuntimeError(f'{unit.name} 的售卖未确认')
        self.units.pop(identifier)
        self.bench.pop(identifier, None)
        return self.field_check(self.game['shot'](), after_field_sale=unit.tile is not None)

    def leave_battle(self, screen, *, recovering):
        game = self.game
        confirm = self.exit_confirmation(screen, abandon=False)
        if confirm is None:
            if recovering:
                cancel = game['find'](game['ocr'](screen, (300, 0, 1700, 820)), '点击取消')
                if cancel:
                    game['tap'](*cancel[:2], 0.5)
            game['tap'](85, 74, 0.7)

            def leave_or_result(image):
                button = self.exit_confirmation(image, abandon=False)
                if button:
                    return button, False
                if self.return_home_button(image):
                    return None, True
                return None

            confirm, result = self.wait_for_screen(
                leave_or_result,
                '未识别战场的「暂时离开模拟」确认框',
            )
            if result:
                return False
        self.original_log('[刷晋升] 点击「暂时离开」的确认按钮，等待大厅加载。')
        game['tap'](*confirm, EXIT_LOBBY_DELAY)
        return True

    def request_abandon(self, screen, *, recovering):
        """Return false when the current screen is a lobby with no unfinished match."""
        confirm = self.exit_confirmation(screen, abandon=True)
        if confirm:
            self.abandon_match(None, confirm=confirm)
            return True
        if self.abandon_requested:
            return True

        buttons = self.lobby_buttons(screen)
        if 'reconnect' not in buttons:
            if 'start' in buttons:
                return False
            if not self.leave_battle(screen, recovering=recovering):
                return True

        def lobby_or_result(image):
            current = self.lobby_buttons(image)
            if 'reconnect' in current and 'abandon' in current:
                return current['abandon'], False
            if self.return_home_button(image):
                return None, True
            return None

        button, result = self.wait_for_screen(
            lobby_or_result, '未识别大厅的「断线重连」和「放弃模拟」按钮',
            timeout=EXIT_LOBBY_TIMEOUT,
        )
        if not result:
            self.abandon_match(button)
        return True

    def confirm_exit(self, completed_limit, *, recovering=False):
        game = self.game
        screen = game['shot']()
        if not self.return_home_button(screen) and not self.request_abandon(screen, recovering=recovering):
            if not recovering:
                raise RuntimeError('已回到大厅，但未确认本局结算')
            self.original_log('[刷晋升] 已回到可开局的大厅，本局无可核对的结算记录。')
            self.reset()
            return
        if recovering and not self.abandon_requested:
            # A naturally completed result is processed by the bundled main loop.
            return

        def confirmed_round(screen):
            if not self.return_home_button(screen):
                return None
            completed = game['numrec'](screen, RESULT_ROUND_BOX)
            if completed == completed_limit:
                return completed
            if recovering:
                if completed is not None and 0 <= completed < completed_limit:
                    return completed
                if completed_limit == 0 and completed is None:
                    return 0
            return None

        # Leave this result on screen for the bundled main loop to log and close.
        expected = f'0～{completed_limit}' if recovering and completed_limit else str(completed_limit)
        try:
            self.exited_round = self.wait_for_screen(
                confirmed_round, f'等待结算 {EXIT_SETTLEMENT_TIMEOUT} 秒，未确认已结算轮次（应为 {expected}）',
                timeout=EXIT_SETTLEMENT_TIMEOUT,
            )
        except RuntimeError:
            if not recovering or not self.return_home_button(game['shot']()):
                raise
            self.exited_round = 0
            self.original_log('[刷晋升] 已确认结算画面，但未核对到已完成轮次，本局不计入，继续重开。')

    def _prep_phase(self, board, image):
        game = self.game
        round_number = game['numrec'](image, ROUND_BOX)
        if round_number is None:
            raise RuntimeError('当前休整回合号码未识别')
        if round_number == board.round_done:
            return
        if round_number != self.round_number + 1:
            raise RuntimeError('接不上当前回合的干员记录，请从大厅开新局')
        if round_number == LAST_ROUND:
            self.round_number = round_number
            self.original_log(f'[刷晋升] R{round_number} {FINAL_EXIT_REASON}')
            self.confirm_exit(round_number - 1)
            board.round_done = round_number
            board.prep_done = False
            return
        screen = self.field_check(image)
        hp = game['numrec'](screen, HP_BOX)
        if self.round_number == 0:
            if not self.strategy_confirmed or hp != 40:
                raise RuntimeError('首轮未确认歌利亚和 40 生命，请从大厅开新局')
            if self.shop_level(screen) != 1:
                time.sleep(NAME_RETRY_DELAY)
                screen = self.field_check(game['shot']())
                if self.shop_level(screen) != 1:
                    raise RuntimeError('首轮商店等级未识别为Ⅰ级')
            if not game['LAYOUT']:
                raise RuntimeError('未加载部署格坐标')
        if not hp or not 0 < hp <= 40:
            raise RuntimeError('当前目标生命未识别')
        self.round_number = round_number
        self.rejected_drop_points.clear()
        if self.coin_held:
            target = next((unit.tile for unit in self.units.values() if unit.tile is not None), None)
            if target is not None:
                screen = self.redeem_coin(screen, target)
        paid_refreshes = 0
        freeze_refresh_used = False
        final_promotion_target = None
        final_promotion_done = False
        blocked_tiles = set()
        screen, offers, rewards = self.read_shop(screen)
        funds = game['read_gold'](screen)
        for _ in range(65):
            if funds is None and not rewards:
                raise RuntimeError('当前资金未识别')
            state = Snapshot(round_number, funds or 0, hp, 10 - len(self.bench),
                             phase='reward' if rewards else 'prep', units=tuple(self.units.values()),
                             shop=offers, rewards=rewards, paid_refreshes=paid_refreshes,
                             blocked_tiles=tuple(blocked_tiles),
                             freeze_refresh_used=freeze_refresh_used, coin_held=self.coin_held,
                             final_promotion_target=final_promotion_target,
                             final_promotion_done=final_promotion_done)
            action = decide(state)
            self.original_log(f'[刷晋升] R{round_number} {action.reason}')
            if action.kind in ('buy', 'claim_reward'):
                card = next(offer for offer in (rewards or offers) if offer.slot == action.slot)
                promotions_before = self.rounds[round_number]
                offers, rewards, funds = self.purchase(card, funds)
                if (action.kind == 'buy' and card.key[1] == final_promotion_target
                        and self.rounds[round_number] > promotions_before):
                    final_promotion_target = None
                    final_promotion_done = True
                blocked_tiles.clear()
            elif action.kind in ('deploy', 'move'):
                if self.deploy(action.unit_id, action.tile):
                    blocked_tiles.clear()
                    if self.coin_held:
                        screen = self.redeem_coin(game['shot'](), action.tile)
                        screen, offers, rewards = self.read_shop(screen)
                        funds = game['read_gold'](screen)
                else:
                    blocked_tiles.add(action.tile)
                    self.original_log(f'[刷晋升] R{round_number} {action.tile} 本次未部署成功，检查其他格子')
            elif action.kind == 'sell':
                screen, offers, rewards = self.read_shop(self.sell(action.unit_id))
                balance = game['read_gold'](screen)
                if action.promotion_target is not None:
                    if balance is None or balance <= funds:
                        raise RuntimeError('第8轮筹资售卖后，未确认资金增加')
                    final_promotion_target = action.promotion_target
                funds = balance
                blocked_tiles.clear()
            elif action.kind == 'refresh':
                offers, rewards, balance = self.refresh_shop(funds, offers)
                paid_refreshes += balance < funds
                freeze_refresh_used = freeze_refresh_used or funds <= UNIT_COST
                funds = balance
            elif action.kind in ('freeze', 'ready'):
                if action.kind == 'freeze':
                    if action.slot is not None:
                        self.buy_coin(action.slot)
                    button = self.text(game['shot'](), FREEZE_BOX)
                    if not any(word in button for word in FROZEN_LABELS):
                        if '冻结' not in button:
                            raise RuntimeError('冻结按钮未识别')
                        game['tap'](1565, 689, 0.6)
                        after_freeze = self.text(game['shot'](), FREEZE_BOX)
                        if not any(word in after_freeze for word in FROZEN_LABELS):
                            raise RuntimeError('冻结后未确认按钮状态')
                    self.original_log(f'[刷晋升] R{round_number} 已确认商店冻结，完成该轮休整')
                board.round_done = round_number
                board.prep_done = True
                game['tap'](*game['READY'], 1.5)
                board.last_ready = time.time()
                game['handle_popups']()
                return
            else:
                raise RuntimeError(action.reason)
        raise RuntimeError('休整动作超过上限')

    def prep_phase(self, board, image):
        try:
            self._prep_phase(board, image)
        except Exception as exc:
            self.original_log(f'[刷晋升] 休整报错，尝试退出本局后重开：{type(exc).__name__}: {exc}')
            board.prep_done = False
            try:
                completed_limit = min(max(0, board.round_done), LAST_ROUND - 1)
                self.confirm_exit(completed_limit, recovering=True)
            except Exception as recovery_error:
                message = f'[刷晋升] 自动重开失败，已停止：{recovery_error}'
                self.original_log(message)
                raise SystemExit(message) from recovery_error


def install(game):
    run = PromotionRun(game)
    run.original_log(f'[刷晋升] 极速模拟 · 歌利亚 · Ⅰ级店 · 目标 {run.target} 次，脚本已结算 {run.total} 次')
    if run.total >= run.target:
        raise SystemExit('[刷晋升] 脚本记录已达到目标，请在游戏内核对。')
    run.abandon_interrupted_at_start()
    game.update(pick_squad=run.pick_squad, pick_strategy=run.pick_strategy,
                prep_phase=run.prep_phase, log=run.log, save=lambda *_: None,
                load_done=set, save_done=lambda _: None)


def replay(game, gpu):
    path = os.environ['GF_REPLAY_PATH']
    img = game['cv2'].imdecode(game['np'].fromfile(path, dtype=game['np'].uint8), 1)
    if img is None or img.shape[:2] != (1080, 1920):
        raise ValueError('回放需要 1920x1080 的商店截图')
    game['ocr_engine'] = gpu.read_image(game, img, bool)
    run = PromotionRun(game)
    buttons = run.lobby_buttons(img)
    confirm = run.exit_confirmation(img, abandon=True)
    leave = None if confirm else run.exit_confirmation(img, abandon=False)
    home = run.return_home_button(img)
    if buttons or confirm or leave or home:
        print(json.dumps({'lobby_buttons': buttons, 'abandon_confirmation': confirm,
                          'leave_confirmation': leave, 'return_home': home,
                          'completed_round': game['numrec'](img, RESULT_ROUND_BOX) if home else None},
                         ensure_ascii=False, default=lambda value: value.item()), flush=True)
        return
    game['load_layout']()
    if '展开' in run.text(img, SHOP_TOGGLE_BOX):
        slots, tiles = run.collapsed_field(img)
        print(json.dumps({'shop': '收起', 'round': game['numrec'](img, ROUND_BOX),
                          'slots': slots, 'field_rank_badges': sorted(tiles)},
                         ensure_ascii=False, default=lambda value: value.item()), flush=True)
        return
    raw = run.screen.shop_cards(img)
    offers, rewards = cards_from_screen(raw, run.roster)
    reward_action = decide(Snapshot(1, 0, 40, 10, phase='reward', rewards=rewards)) if rewards else None
    preferred = next((reward.name for reward in rewards if reward.slot == reward_action.slot), None) if reward_action else None
    tiles = (game['LAYOUT'] or {}).get('prep', {})
    print(json.dumps({
        'round': game['numrec'](img, ROUND_BOX), 'hp': game['numrec'](img, HP_BOX),
        'funds': game['numrec'](img, game['GOLD_BOX']), 'slots': run.screen.field_slots(img),
        'free_refreshes': run.free_refreshes(img),
        'template_slots': run.screen.field_slots_from_template(img),
        'legacy_slots': game['slots_left'](img), 'label_slots': run.screen.field_slots_from_label(img),
        'bench_positions': sorted(run.bench_slots(img), reverse=True),
        'field_rank_badges': [tile for tile in tiles if run.tile_has_rank_badge(img, tile)],
        'level': run.shop_level(img), 'level_text': run.text(img, SHOP_LEVEL_BOX),
        'offers': [offer.name for offer in offers], 'rewards': [offer.name for offer in rewards],
        'preferred_reward': preferred,
        'raw': raw,
    }, ensure_ascii=False, default=lambda value: value.item()), flush=True)
