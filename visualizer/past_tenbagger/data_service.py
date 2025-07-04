from typing import Dict, List, Tuple, Optional, Any
import pandas as pd
import json
import logging
import glob
import os
import subprocess
from pathlib import Path

from .config import (
    IPO_REPORTS_DIR,
    COMPARISON_DIR,
    METRIC_ALIASES
)

# カラーフォーマッターの設定
class ColoredFormatter(logging.Formatter):
    """ログメッセージに色を付けるフォーマッター"""
    
    COLORS = {
        'WARNING': '\033[0;33m',  # 黄色
        'ERROR': '\033[0;31m',    # 赤
        'CRITICAL': '\033[0;35m', # マゼンタ
    }

    def format(self, record):
        # ログレベルに応じた色を選択（DEBUGとINFOは色なし）
        color = self.COLORS.get(record.levelname, '')
        # 元のメッセージをフォーマット
        message = super().format(record)
        # 色を適用（色が設定されている場合のみリセット）
        if color:
            return f"{color}{message}\033[0m"
        return message

# ロガーの設定
logger = logging.getLogger(__name__)
handler = logging.StreamHandler()
formatter = ColoredFormatter('%(asctime)s - %(name)s - %(levelname)s - %(message)s')
handler.setFormatter(formatter)
logger.addHandler(handler)
logger.setLevel(logging.INFO)

class DataService:
    @staticmethod
    def get_company_code_name_map() -> Dict[str, str]:
        """すべての企業コードと名前のマッピングを取得"""
        company_map = {}
        
        try:
            for company_dir in glob.glob(f"{IPO_REPORTS_DIR}/*"):
                dir_name = os.path.basename(company_dir)
                if '_' in dir_name:
                    code, name = dir_name.split('_', 1)
                    company_map[code] = name
            
            logger.info(f"企業数: {len(company_map)}")
        except Exception as e:
            logger.error(f"企業コード・名前マッピングの取得中にエラー: {e}", exc_info=True)
        
        return company_map

    @staticmethod
    def get_competitors(company_code: str) -> List[Dict[str, str]]:
        """指定された企業コードの競合企業リストを取得"""
        comparison_files = sorted(glob.glob(f"{COMPARISON_DIR}/companies_*.tsv"), reverse=True)
        
        if not comparison_files:
            return []
        
        try:
            # 全てのファイルを読み込んで統合
            dfs = [pd.read_csv(file, sep='\t') for file in comparison_files]
            df = pd.concat(dfs, ignore_index=True)
            
            # 重複を削除（同じコードの企業は最新のデータを保持）
            df = df.drop_duplicates(subset=['コード'], keep='first')
            
            # 企業コードに一致する行を探す
            company_row = df[df['コード'].astype(str) == str(company_code)]
            
            if company_row.empty or pd.isna(company_row['競合リスト'].values[0]):
                return []
            
            return json.loads(company_row['競合リスト'].values[0])
        except Exception as e:
            logger.error(f"競合企業リストの取得中にエラー: {e}", exc_info=True)
            return []

    @staticmethod
    def get_company_data(company_code: str) -> Tuple[Optional[pd.DataFrame], Optional[str]]:
        """指定された企業コードの財務データを取得"""
        company_map = DataService.get_company_code_name_map()
        
        if company_code not in company_map:
            logger.error(f"企業コード {company_code} が見つかりません")
            return None, "企業が見つかりません"
        
        company_name = company_map[company_code]
        company_dir = f"{IPO_REPORTS_DIR}/{company_code}_{company_name}"
        report_files = glob.glob(f"{company_dir}/annual_securities_reports/*.tsv")
        
        if not report_files:
            logger.error(f"企業 {company_code}_{company_name} の有価証券報告書が見つかりません")
            return None, "有価証券報告書が見つかりません"
        
        all_data = []
        for file_path in sorted(report_files):
            try:
                df = DataService._read_financial_file(file_path)
                if df is not None:
                    all_data.append(df)
            except Exception as e:
                logger.error(f"ファイル読み込みエラー {file_path}: {e}", exc_info=True)
        
        if not all_data:
            return None, "データの読み込みに失敗しました"
        
        try:
            combined_data = pd.concat(all_data, ignore_index=True)
            return combined_data, None
        except Exception as e:
            logger.error(f"データ結合エラー: {e}", exc_info=True)
            return None, "データの結合に失敗しました"

    @staticmethod
    def _read_financial_file(file_path: str) -> Optional[pd.DataFrame]:
        """財務データファイルを読み込む（堅牢なエンコーディング検出付き）"""
        try:
            import chardet
            
            # 方法1: chardetを使用した自動検出
            def detect_encoding_with_chardet(file_path):
                try:
                    with open(file_path, 'rb') as f:
                        raw_data = f.read(10000)  # 最初の10KBを読み取り
                    detected = chardet.detect(raw_data)
                    if detected and detected['encoding'] and detected['confidence'] > 0.7:
                        logger.info(f"chardetで検出されたエンコーディング: {detected['encoding']} (信頼度: {detected['confidence']:.2f})")
                        return detected['encoding']
                except Exception as e:
                    logger.debug(f"chardetでのエンコーディング検出に失敗: {e}")
                return None
            
            # 方法2: ファイルの先頭バイトからBOMを検出
            def detect_encoding_from_bom(file_path):
                try:
                    with open(file_path, 'rb') as f:
                        bom = f.read(4)
                    
                    if bom.startswith(b'\xff\xfe\x00\x00'):
                        return 'utf-32-le'
                    elif bom.startswith(b'\x00\x00\xfe\xff'):
                        return 'utf-32-be'
                    elif bom.startswith(b'\xff\xfe'):
                        return 'utf-16-le'
                    elif bom.startswith(b'\xfe\xff'):
                        return 'utf-16-be'
                    elif bom.startswith(b'\xef\xbb\xbf'):
                        return 'utf-8-sig'  # UTF-8 with BOM
                except Exception as e:
                    logger.debug(f"BOMでのエンコーディング検出に失敗: {e}")
                return None
            
            # 方法3: fileコマンドを使用
            def detect_encoding_with_file_command(file_path):
                try:
                    result = os.popen(f'file "{file_path}"').read()
                    if 'UTF-16' in result:
                        if 'little-endian' in result:
                            return 'utf-16-le'
                        elif 'big-endian' in result:
                            return 'utf-16-be'
                        else:
                            return 'utf-16'
                    elif 'UTF-8' in result:
                        return 'utf-8'
                    elif 'ASCII' in result:
                        return 'ascii'
                except Exception as e:
                    logger.debug(f"fileコマンドでのエンコーディング検出に失敗: {e}")
                return None
            
            # エンコーディング候補のリスト（優先順位順）
            encoding_candidates = []
            
            # 1. BOM検出
            bom_encoding = detect_encoding_from_bom(file_path)
            if bom_encoding:
                encoding_candidates.append(bom_encoding)
            
            # 2. chardet検出
            chardet_encoding = detect_encoding_with_chardet(file_path)
            if chardet_encoding:
                encoding_candidates.append(chardet_encoding)
            
            # 3. fileコマンド検出
            file_encoding = detect_encoding_with_file_command(file_path)
            if file_encoding:
                encoding_candidates.append(file_encoding)
            
            # 4. 一般的なエンコーディングを追加
            common_encodings = ['utf-8', 'utf-16', 'utf-16-le', 'utf-16-be', 'shift_jis', 'cp932', 'euc-jp', 'iso-2022-jp']
            for enc in common_encodings:
                if enc not in encoding_candidates:
                    encoding_candidates.append(enc)
            
            # エンコーディングを順番に試行
            for encoding in encoding_candidates:
                try:
                    logger.info(f"エンコーディング '{encoding}' で読み込み試行中: {os.path.basename(file_path)}")
                    
                    # ファイルを読み込む
                    df = pd.read_csv(file_path, sep='\t', encoding=encoding, on_bad_lines='skip')
                    
                    # カラム名を標準化
                    standard_columns = ['要素ID', '項目名', 'コンテキストID', '相対年度', '連結・個別',
                                      '期間・時点', 'ユニットID', '単位', '値']
                    
                    if len(df.columns) >= len(standard_columns):
                        df.columns = standard_columns + list(df.columns[len(standard_columns):])
                    else:
                        df = df.reindex(columns=standard_columns)
                    
                    # ファイル名から年度を抽出して追加
                    year = os.path.basename(file_path).split('_')[0][:4]
                    df['年度'] = year
                    
                    # 読み込み成功
                    logger.info(f"ファイル読み込み成功 (エンコーディング: {encoding}): {os.path.basename(file_path)}")
                    return df
                    
                except UnicodeDecodeError as e:
                    logger.debug(f"エンコーディング '{encoding}' で読み込み失敗: {e}")
                    continue
                except Exception as e:
                    logger.debug(f"エンコーディング '{encoding}' で予期しないエラー: {e}")
                    continue
            
            # すべてのエンコーディングで失敗した場合
            logger.error(f"すべてのエンコーディングで読み込みに失敗しました: {file_path}")
            return None
            
        except ImportError:
            # chardetがインストールされていない場合の旧来の方法
            logger.warning("chardetライブラリがインストールされていません。基本的なエンコーディング検出を使用します。")
            
            encodings_to_try = ['utf-8', 'utf-16-le', 'utf-16-be', 'shift-jis', 'cp932']
            
            for encoding in encodings_to_try:
                try:
                    df = pd.read_csv(file_path, sep='\t', encoding=encoding, on_bad_lines='skip')
                    
                    # カラム名を標準化
                    standard_columns = ['要素ID', '項目名', 'コンテキストID', '相対年度', '連結・個別',
                                      '期間・時点', 'ユニットID', '単位', '値']
                    
                    if len(df.columns) >= len(standard_columns):
                        df.columns = standard_columns + list(df.columns[len(standard_columns):])
                    else:
                        df = df.reindex(columns=standard_columns)
                    
                    # ファイル名から年度を抽出して追加
                    year = os.path.basename(file_path).split('_')[0][:4]
                    df['年度'] = year
                    
                    return df
                except UnicodeDecodeError:
                    continue
                except Exception as e:
                    logger.error(f"ファイル読み込みエラー {file_path}: {e}", exc_info=True)
                    continue
            
            logger.error(f"すべてのエンコーディングで読み込みに失敗しました {file_path}")
            return None
            
        except Exception as e:
            logger.error(f"財務データファイルの読み込み中に予期しないエラー: {e}", exc_info=True)
            return None

    @staticmethod
    def extract_metrics(data: Optional[pd.DataFrame], metrics_list: List[str] = None) -> Dict[str, Dict[str, float]]:
        """指定された指標のデータを抽出"""
        if data is None:
            logger.warning("データがNoneのため、指標を抽出できません")
            return {}
        
        # デフォルトではMETRIC_ALIASESのキーを使用
        if metrics_list is None:
            metrics_list = list(METRIC_ALIASES.keys())
        
        metrics_data = {}
        try:
            for metric in metrics_list:
                metric_data = DataService._extract_single_metric(data, metric)
                if metric_data:
                    metrics_data[metric] = metric_data
            
            # 営業利益率を計算
            DataService._calculate_operating_margin(metrics_data)
            
            # PEGレシオを計算
            DataService._calculate_peg_ratio(metrics_data)
            
            # ROAを計算（有価証券報告書から直接取得できない場合のバックアップ）
            DataService._calculate_roa(metrics_data)

            # 従業員一人当たりの営業利益を計算
            DataService._calculate_operating_profit_per_employee(metrics_data)
            
        except Exception as e:
            logger.error(f"指標抽出中に予期しないエラー: {e}", exc_info=True)
        
        return metrics_data

    @staticmethod
    def _extract_single_metric(data: pd.DataFrame, metric: str) -> Dict[str, float]:
        """単一の指標データを抽出"""
        try:
            aliases = METRIC_ALIASES.get(metric, [])
            metric_rows = pd.DataFrame()
            
            for alias in aliases:
                alias_rows = data[data['要素ID'].str.contains(alias, na=False, regex=False)]
                if not alias_rows.empty:
                    metric_rows = pd.concat([metric_rows, alias_rows])
            
            if metric_rows.empty:
                return {}
            
            years_data = {}
            for _, row in metric_rows.iterrows():
                if '年度' not in row or '値' not in row:
                    continue
                
                year = row['年度']
                value = row['値']
                
                if isinstance(value, pd.Series):
                    non_nan_values = value.dropna()
                    if not non_nan_values.empty:
                        value = non_nan_values.iloc[0]
                    else:
                        continue
                
                try:
                    if pd.notna(value) and value != '－' and value != '-':
                        value = float(value.replace(',', '')) if isinstance(value, str) else float(value)
                        years_data[year] = value
                except (ValueError, AttributeError) as e:
                    logger.warning(f"値の変換エラー: {value}, {e}")
            
            return years_data
        except Exception as e:
            logger.error(f"指標 '{metric}' の抽出中にエラー: {e}", exc_info=True)
            return {}

    @staticmethod
    def _calculate_operating_margin(metrics_data: Dict[str, Dict[str, float]]) -> None:
        """営業利益率を計算"""
        if '営業利益率' not in metrics_data and '営業利益' in metrics_data and '売上高' in metrics_data:
            try:
                operating_profit = metrics_data['営業利益']
                sales = metrics_data['売上高']
                
                operating_profit_ratio = {}
                for year in set(operating_profit.keys()) & set(sales.keys()):
                    if sales[year] != 0:
                        operating_profit_ratio[year] = (operating_profit[year] / sales[year]) * 100
                
                if operating_profit_ratio:
                    metrics_data['営業利益率'] = operating_profit_ratio
                    logger.info(f"営業利益率を計算しました。{len(operating_profit_ratio)} 年分のデータがあります。")
            except Exception as e:
                logger.error(f"営業利益率の計算中にエラー: {e}", exc_info=True)

    @staticmethod
    def _calculate_peg_ratio(metrics_data: Dict[str, Dict[str, float]]) -> None:
        """PEGレシオを計算（PER / EPSの成長率）"""
        if 'PEGレシオ（PER / EPS成長率）' not in metrics_data and 'PER（株価収益率）' in metrics_data and '１株当たり当期純利益（EPS）' in metrics_data:
            try:
                per = metrics_data['PER（株価収益率）']
                eps = metrics_data['１株当たり当期純利益（EPS）']
                
                # EPSの成長率を計算
                eps_growth_rates = DataService.calculate_growth_rate(eps)
                
                peg_ratio = {}
                for year in set(per.keys()) & set(eps_growth_rates.keys()):
                    if eps_growth_rates[year] > 0:  # 成長率がプラスの場合のみ計算
                        peg_ratio[year] = per[year] / eps_growth_rates[year]
                
                if peg_ratio:
                    metrics_data['PEGレシオ（PER / EPS成長率）'] = peg_ratio
                    logger.info(f"PEGレシオを計算しました。{len(peg_ratio)} 年分のデータがあります。")
            except Exception as e:
                logger.error(f"PEGレシオの計算中にエラー: {e}", exc_info=True)

    @staticmethod
    def calculate_growth_rate(data: Dict[str, float]) -> Dict[str, float]:
        """年度ごとのデータから成長率を計算する"""
        if not data or len(data) < 2:
            return {}
        
        sorted_years = sorted(data.keys())
        growth_rates = {}
        
        for i in range(1, len(sorted_years)):
            current_year = sorted_years[i]
            prev_year = sorted_years[i-1]
            
            current_value = data.get(current_year)
            prev_value = data.get(prev_year)
            
            if current_value is not None and prev_value is not None and prev_value != 0:
                growth_rate = ((current_value - prev_value) / abs(prev_value)) * 100
                growth_rates[current_year] = growth_rate
        
        return growth_rates

    @staticmethod
    def _calculate_roa(metrics_data: Dict[str, Dict[str, float]]) -> None:
        """ROA（総資産利益率）を計算（当期純利益 / 総資産）"""
        if 'ROA（総資産利益率）' not in metrics_data and '当期純利益' in metrics_data and '総資産' in metrics_data:
            try:
                net_income = metrics_data['当期純利益']
                total_assets = metrics_data['総資産']
                
                roa = {}
                for year in set(net_income.keys()) & set(total_assets.keys()):
                    if total_assets[year] != 0:
                        roa[year] = (net_income[year] / total_assets[year]) * 100
                
                if roa:
                    metrics_data['ROA（総資産利益率）'] = roa
                    logger.info(f"ROA（総資産利益率）を計算しました。{len(roa)} 年分のデータがあります。")
            except Exception as e:
                logger.error(f"ROA（総資産利益率）の計算中にエラー: {e}", exc_info=True)

    @staticmethod
    def _calculate_operating_profit_per_employee(metrics_data: Dict[str, Dict[str, float]]) -> None:
        """従業員一人当たりの営業利益を計算"""
        if '営業利益' in metrics_data and '従業員数' in metrics_data:
            operating_profit = metrics_data['営業利益']
            employees = metrics_data['従業員数']
            
            result = {}
            for year in operating_profit:
                if year in employees and employees[year] > 0:
                    result[year] = operating_profit[year] / employees[year]
            
            if result:
                metrics_data['従業員一人当たり営業利益'] = result
                logger.info("従業員一人当たり営業利益を計算しました")

    @staticmethod
    def get_officers_info(company_code: str) -> Optional[str]:
        """指定された企業コードの役員情報を取得"""
        company_map = DataService.get_company_code_name_map()
        
        if company_code not in company_map:
            logger.error(f"企業コード {company_code} が見つかりません")
            return None
        
        company_name = company_map[company_code]
        company_dir = f"{IPO_REPORTS_DIR}/{company_code}_{company_name}"
        
        # 年次報告書ディレクトリを確認
        annual_reports_dir = f"{company_dir}/annual_securities_reports"
        if not os.path.exists(annual_reports_dir):
            logger.warning(f"年次報告書ディレクトリが見つかりません: {annual_reports_dir}")
            return None
        
        # 最も古い年次報告書を取得（reversed=Falseで昇順ソート）
        report_files = sorted(glob.glob(f"{annual_reports_dir}/*.tsv"), reverse=False)
        if not report_files:
            logger.warning(f"年次報告書が見つかりません: {annual_reports_dir}")
            return None
        
        oldest_report = report_files[0]
        
        try:
            # TSVファイルを読み込む
            df = DataService._read_tsv_with_fallback_encodings(oldest_report)
            if df is None:
                return None
            
            # 役員情報を検索
            officers_row = df[df['要素ID'] == 'jpcrp_cor:InformationAboutOfficersTextBlock']
            
            if officers_row.empty:
                logger.warning(f"役員情報が見つかりません: {oldest_report}")
                return None
            
            # 役員情報のテキストを取得
            officers_text = officers_row['値'].values[0]
            
            # HTMLとして整形
            officers_html = officers_text.replace('\n', '<br>')
            
            return officers_html
            
        except Exception as e:
            logger.error(f"役員情報の取得中にエラー: {e}", exc_info=True)
            return None

    @staticmethod
    def get_business_description(company_code: str) -> Optional[str]:
        """指定された企業コードの事業の内容を取得"""
        company_map = DataService.get_company_code_name_map()
        
        if company_code not in company_map:
            logger.error(f"企業コード {company_code} が見つかりません")
            return None
        
        company_name = company_map[company_code]
        company_dir = f"{IPO_REPORTS_DIR}/{company_code}_{company_name}"
        
        # 年次報告書ディレクトリを確認
        annual_reports_dir = f"{company_dir}/annual_securities_reports"
        if not os.path.exists(annual_reports_dir):
            logger.warning(f"年次報告書ディレクトリが見つかりません: {annual_reports_dir}")
            return None
        
        # 最も古い年次報告書を取得（reversed=Falseで昇順ソート）
        report_files = sorted(glob.glob(f"{annual_reports_dir}/*.tsv"), reverse=False)
        if not report_files:
            logger.warning(f"年次報告書が見つかりません: {annual_reports_dir}")
            return None
        
        oldest_report = report_files[0]
        
        try:
            # TSVファイルを読み込む
            df = DataService._read_tsv_with_fallback_encodings(oldest_report)
            if df is None:
                return None
            
            # 事業の内容を検索
            business_row = df[df['要素ID'] == 'jpcrp_cor:DescriptionOfBusinessTextBlock']
            
            if business_row.empty:
                logger.warning(f"事業の内容が見つかりません: {oldest_report}")
                return None
            
            # 事業の内容のテキストを取得
            business_text = business_row['値'].values[0]
            
            # HTMLとして整形
            business_html = business_text.replace('\n', '<br>')
            
            return business_html
            
        except Exception as e:
            logger.error(f"事業の内容の取得中にエラー: {e}", exc_info=True)
            return None

    @staticmethod
    def _read_tsv_with_fallback_encodings(file_path: str) -> Optional[pd.DataFrame]:
        """複数のエンコーディングを試行してTSVファイルを読み込む"""
        encodings_to_try = ['utf-8', 'utf-16-le', 'utf-16-be', 'shift-jis', 'cp932']
        
        for encoding in encodings_to_try:
            try:
                df = pd.read_csv(file_path, sep='\t', encoding=encoding, on_bad_lines='skip')
                return df
            except UnicodeDecodeError:
                continue
            except Exception as e:
                logger.warning(f"エンコーディング {encoding} での読み込み中にエラー: {e}")
                continue
        
        logger.error(f"すべてのエンコーディングで読み込みに失敗しました: {file_path}")
        return None