#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
豆瓣电影上映日历 - ICS 生成工具
从豆瓣获取当月及下月电影上映数据，生成苹果日历可订阅的 ICS 文件

数据来源：
  - 主数据源：豆瓣影院即将上映页面（cinema/later）
  - 补充数据源：豆瓣搜索接口（search_subjects）
"""

import os
import re
import uuid
import glob
import logging
import requests
from datetime import datetime, timedelta, date
from bs4 import BeautifulSoup
from icalendar import Calendar, Event, vText

# ---------------------------------------------------------------------------
# 配置
# ---------------------------------------------------------------------------

logging.basicConfig(
    level=logging.INFO,
    format='%(asctime)s [%(levelname)s] %(message)s',
    datefmt='%Y-%m-%d %H:%M:%S'
)
logger = logging.getLogger(__name__)

BASE_DIR = os.path.dirname(os.path.abspath(__file__))

HEADERS = {
    'User-Agent': (
        'Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) '
        'AppleWebKit/537.36 (KHTML, like Gecko) '
        'Chrome/120.0.0.0 Safari/537.36'
    ),
    'Accept': 'text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8',
    'Accept-Language': 'zh-CN,zh;q=0.9,en;q=0.8',
    'Referer': 'https://movie.douban.com/',
}

SEARCH_API = 'https://movie.douban.com/j/search_subjects'
CINEMA_LATER_URL = 'https://movie.douban.com/cinema/later/beijing/'


# ---------------------------------------------------------------------------
# 豆瓣数据抓取
# ---------------------------------------------------------------------------

class DoubanMovieScraper:
    """豆瓣电影数据抓取器"""

    def __init__(self):
        self.session = requests.Session()
        self.session.headers.update(HEADERS)

    # ------------------------------------------------------------------
    # 主数据源：影院即将上映页面
    # ------------------------------------------------------------------

    def fetch_cinema_later(self) -> list[dict]:
        """
        从豆瓣影院即将上映页面抓取电影数据。
        页面地址：https://movie.douban.com/cinema/later/beijing/
        返回格式：[{title, id, date, genres, region, want_count, cover}, ...]
        """
        logger.info('正在抓取豆瓣影院即将上映页面 ...')
        try:
            resp = self.session.get(CINEMA_LATER_URL, timeout=30)
            resp.raise_for_status()
        except Exception as exc:
            logger.error('抓取影院即将上映页面失败: %s', exc)
            return []

        soup = BeautifulSoup(resp.text, 'lxml')
        items = soup.select('.item')
        if not items:
            logger.warning('影院即将上映页面未找到电影条目')
            return []

        logger.info('页面共找到 %d 个条目', len(items))
        movies = []
        now = datetime.now()

        for item in items:
            try:
                movie = self._parse_cinema_item(item, now)
                if movie:
                    movies.append(movie)
            except Exception as exc:
                logger.debug('解析条目失败: %s', exc)
                continue

        logger.info('成功解析 %d 部电影', len(movies))
        return sorted(movies, key=lambda m: m['date'])

    def _parse_cinema_item(self, item, now: datetime) -> dict | None:
        """解析影院即将上映页面中的单个电影条目"""
        # 标题
        title_el = item.select_one('h3') or item.select_one('.title')
        title = title_el.get_text(strip=True) if title_el else ''
        if not title:
            return None

        # 从链接提取 subject id
        link = item.select_one('a.thumb') or item.select_one('a')
        href = link.get('href', '') if link else ''
        sid_match = re.search(r'/subject/(\d+)', href)
        movie_id = sid_match.group(1) if sid_match else ''

        # 列表信息：<ul> 中的 <li> 依次为 上映日期、类型、地区、想看人数
        lis = item.select('ul li')
        li_texts = [li.get_text(strip=True) for li in lis]

        release_date = None
        genres = ''
        region = ''

        if len(li_texts) >= 1:
            release_date = self._parse_later_date(li_texts[0], now)
        if len(li_texts) >= 2:
            genres = li_texts[1]
        if len(li_texts) >= 3:
            region = li_texts[2]

        if release_date is None:
            return None

        # 海报图片
        cover = ''
        img = item.select_one('img')
        if img:
            cover = img.get('src', '') or img.get('data-src', '') or ''

        return {
            'id':         movie_id,
            'title':      title,
            'date':       release_date,
            'rating':     '',
            'cover':      cover,
            'genres':     genres,
            'region':     region,
            'actors':     '',
            'directors':  '',
        }

    @staticmethod
    def _parse_later_date(text: str, now: datetime) -> date | None:
        """
        解析 "08月07日" 格式的日期。
        若跨年（上映月份早于当前月份），自动推断为下一年。
        """
        m = re.search(r'(\d{1,2})月(\d{1,2})日', text)
        if not m:
            return None
        month = int(m.group(1))
        day   = int(m.group(2))
        year  = now.year
        # 若上映月份早于当前月份（跨年上映），则年份 +1
        if month < now.month - 1:
            year += 1
        try:
            return date(year, month, day)
        except ValueError:
            return None

    # ------------------------------------------------------------------
    # 补充数据源：搜索接口
    # ------------------------------------------------------------------

    def fetch_by_search(self, year: int, month: int) -> list[dict]:
        """
        使用搜索接口补充指定月份的电影数据。
        尝试多种标签关键词（最新、热门、上映等）获取电影列表。
        """
        all_movies = []
        seen_ids = set()
        tags = ['最新', '热门', '即将上映']

        for tag in tags:
            logger.info('搜索接口 - 标签: %s (%d年%d月)', tag, year, month)
            for page_start in range(0, 100, 20):
                try:
                    resp = self.session.get(
                        SEARCH_API,
                        params={
                            'type': 'movie',
                            'tag': tag,
                            'page_limit': 20,
                            'page_start': page_start,
                        },
                        timeout=20
                    )
                    resp.raise_for_status()
                    data = resp.json()
                    items = data.get('subjects', [])
                    if not items:
                        break

                    for item in items:
                        sid = str(item.get('id', ''))
                        if sid in seen_ids:
                            continue
                        seen_ids.add(sid)

                        all_movies.append({
                            'id':        sid,
                            'title':     item.get('title', '未知'),
                            'date':      None,   # 搜索接口无上映日期
                            'rating':    str(item.get('rate', '')),
                            'cover':     item.get('cover', ''),
                            'genres':    '',
                            'region':    '',
                            'actors':    '',
                            'directors': '',
                        })

                    if len(items) < 20:
                        break
                except Exception as exc:
                    logger.error('搜索接口异常: %s', exc)
                    break

        logger.info('搜索接口共获取 %d 部电影（去重后）', len(all_movies))
        return all_movies


# ---------------------------------------------------------------------------
# ICS 文件生成
# ---------------------------------------------------------------------------

class ICSGenerator:
    """将电影数据转换为 ICS 日历文件"""

    def __init__(self, cal_name: str = '豆瓣电影上映日历'):
        self.cal = Calendar()
        self.cal.add('prodid', '-//Douban Movie Calendar//CN')
        self.cal.add('version', '2.0')
        self.cal.add('calscale', 'GREGORIAN')
        self.cal.add('method', 'PUBLISH')
        self.cal.add('x-wr-calname', cal_name)
        self.cal.add('x-wr-timezone', 'Asia/Shanghai')

    def add_movies(self, movies: list[dict]):
        """批量添加电影事件"""
        count = 0
        for movie in movies:
            if movie.get('date'):
                self._add_movie_event(movie)
                count += 1
        logger.info('已添加 %d 个日历事件', count)

    def _add_movie_event(self, movie: dict):
        """添加单部电影为全天日历事件"""
        event = Event()

        # 标题
        title = movie['title']
        if movie.get('rating') and movie['rating'] not in ('', '0', '0.0'):
            title = f'🎬 {title} ⭐{movie["rating"]}'
        else:
            title = f'🎬 {title}'

        event.add('summary', title)
        event.add('dtstart', movie['date'])
        event.add('dtend',   movie['date'] + timedelta(days=1))

        # 描述信息
        desc_parts = [f'电影：{movie["title"]}']
        if movie.get('rating') and movie['rating'] not in ('', '0', '0.0'):
            desc_parts.append(f'豆瓣评分：{movie["rating"]}')
        if movie.get('genres'):
            desc_parts.append(f'类型：{movie["genres"]}')
        if movie.get('region'):
            desc_parts.append(f'地区：{movie["region"]}')
        if movie.get('directors'):
            desc_parts.append(f'导演：{movie["directors"]}')
        if movie.get('actors'):
            desc_parts.append(f'主演：{movie["actors"]}')
        if movie.get('id'):
            desc_parts.append(f'豆瓣链接：https://movie.douban.com/subject/{movie["id"]}/')

        description = '\n'.join(desc_parts)
        event.add('description', vText(description))
        event['uid'] = f'{movie.get("id", uuid.uuid4().hex)}@douban-movie-calendar'

        self.cal.add_component(event)

    def save(self, filepath: str):
        """将日历数据写入 .ics 文件"""
        os.makedirs(os.path.dirname(filepath), exist_ok=True)
        with open(filepath, 'wb') as f:
            f.write(self.cal.to_ical())
        logger.info('✅ ICS 文件已保存: %s', filepath)


# ---------------------------------------------------------------------------
# 主入口
# ---------------------------------------------------------------------------

def main():
    logger.info('=' * 50)
    logger.info('豆瓣电影日历 ICS 生成工具 启动')
    logger.info('=' * 50)

    now   = datetime.now()
    year  = now.year
    month = now.month

    # 计算下月
    if month == 12:
        next_year, next_month = year + 1, 1
    else:
        next_year, next_month = year, month + 1

    scraper = DoubanMovieScraper()

    # ------------------------------------------------------------------
    # 抓取影院即将上映数据（包含当月和下月）
    # ------------------------------------------------------------------
    all_movies = scraper.fetch_cinema_later()

    # ------------------------------------------------------------------
    # 处理当月
    # ------------------------------------------------------------------
    current_movies = [
        m for m in all_movies
        if m['date'].year == year and m['date'].month == month
    ]
    logger.info('%d年%d月 共 %d 部电影（来自影院页面）', year, month, len(current_movies))

    if current_movies:
        _save_ics(year, month, current_movies)
    else:
        logger.warning('%d年%d月 未获取到电影数据', year, month)

    # ------------------------------------------------------------------
    # 处理下月
    # ------------------------------------------------------------------
    next_movies = [
        m for m in all_movies
        if m['date'].year == next_year and m['date'].month == next_month
    ]
    logger.info('%d年%d月 共 %d 部电影（来自影院页面）', next_year, next_month, len(next_movies))

    if next_movies:
        _save_ics(next_year, next_month, next_movies)
    else:
        logger.warning('%d年%d月 未获取到电影数据', next_year, next_month)

    # ------------------------------------------------------------------
    # 汇总所有月度数据到一个 ICS 文件
    # ------------------------------------------------------------------
    _merge_all_movies()

    logger.info('=' * 50)
    logger.info('所有任务执行完毕！')


def _save_ics(year: int, month: int, movies: list[dict]):
    """将电影数据保存为 ICS 文件到 data/{year}/{month}/movies.ics"""
    month_dir = os.path.join(BASE_DIR, 'data', str(year), f'{month:02d}')
    os.makedirs(month_dir, exist_ok=True)

    gen = ICSGenerator(cal_name=f'豆瓣电影 - {year}年{month}月')
    gen.add_movies(movies)
    gen.save(os.path.join(month_dir, 'movies.ics'))
    logger.info('已保存 %d年%d月 ICS 文件 (%d 部电影)', year, month, len(movies))


def _merge_all_movies():
    """
    扫描 data/ 下所有月度 movies.ics 文件，
    将全部事件汇总到一个 data/movies.ics 供用户订阅。
    """
    data_dir = os.path.join(BASE_DIR, 'data')
    pattern  = os.path.join(data_dir, '*', '*', 'movies.ics')
    ics_files = sorted(glob.glob(pattern))

    if not ics_files:
        logger.warning('未找到任何月度 ICS 文件，跳过汇总')
        return

    gen = ICSGenerator(cal_name='豆瓣电影上映日历')
    total = 0

    for filepath in ics_files:
        try:
            with open(filepath, 'rb') as f:
                source_cal = Calendar.from_ical(f.read())

            for component in source_cal.walk():
                if component.name == 'VEVENT':
                    gen.cal.add_component(component)
                    total += 1

            logger.info('已合并: %s', os.path.relpath(filepath, BASE_DIR))
        except Exception as exc:
            logger.error('读取 %s 失败: %s', filepath, exc)

    summary_path = os.path.join(data_dir, 'movies.ics')
    os.makedirs(data_dir, exist_ok=True)
    with open(summary_path, 'wb') as f:
        f.write(gen.cal.to_ical())

    logger.info('✅ 汇总完成: %s (%d 个事件，来自 %d 个月度文件)',
                summary_path, total, len(ics_files))


if __name__ == '__main__':
    main()
