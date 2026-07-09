"""
高级情感分析模块
集成SnowNLP、jieba分词，支持行业特定词汇
"""
import re
from typing import Dict, List, Tuple
from collections import defaultdict

try:
    from snownlp import SnowNLP
    HAS_SNOWNLP = True
except ImportError:
    HAS_SNOWNLP = False
    print("提示: 安装snownlp可获得更好的情感分析效果: pip install snownlp")

try:
    import jieba
    import jieba.analyse
    HAS_JIEBA = True
except ImportError:
    HAS_JIEBA = False
    print("提示: 安装jieba可获得更好的分词效果: pip install jieba")


class SentimentAnalyzer:
    """高级情感分析器类"""
    
    def __init__(self):
        """初始化情感分析器"""
        
        # ========== 基础情感词库 ==========
        self.positive_words = {
            # 业绩相关
            '增长', '上涨', '突破', '创新高', '利好', '超预期', '盈利', '营收增长',
            '净利润增长', '业绩预增', '扭亏为盈', '分红', '回购', '增持', '预盈',
            '大幅增长', '翻倍', '暴涨', '飙升', '井喷', '爆发', '强劲',
            
            # 行业相关
            '景气', '复苏', '扩张', '需求旺盛', '供不应求', '涨价', '提价',
            '产能释放', '订单增长', '市占率提升', '行业龙头', '技术领先',
            
            # 公司相关
            '优质', '龙头', '垄断', '稀缺', '护城河', '核心竞争力',
            '管理层优秀', '战略清晰', '执行力强', '品牌价值',
            
            # 政策相关
            '政策支持', '补贴', '税收优惠', '产业政策', '国产替代',
            '自主可控', '新基建', '碳中和', '专精特新', '数字经济',
            
            # 市场相关
            '资金流入', '机构加仓', '北向资金', '外资看好', '评级上调',
            '目标价上调', '买入评级', '强烈推荐', '底部反转', '超跌反弹',
            
            # 技术面
            '放量上涨', '突破压力', '金叉', '多头排列', '趋势向上'
        }
        
        self.negative_words = {
            # 业绩相关
            '下跌', '下降', '亏损', '下滑', '低于预期', '业绩预减', '亏损扩大',
            '净利润下降', '营收下滑', '亏损收窄', '不分红', '预亏', '首亏',
            '大幅下降', '暴跌', '闪崩', '崩盘', '腰斩', '爆仓',
            
            # 行业相关
            '产能过剩', '价格战', '竞争加剧', '需求疲软', '库存积压',
            '降价', '降价促销', '开工率不足', '行业寒冬',
            
            # 公司相关
            '暴雷', '造假', '违规', '处罚', '诉讼', '纠纷', '债务',
            '资金链', '商誉减值', '资产减值', '质押', '爆雷', '跑路',
            '财务造假', '内幕交易', '操纵市场',
            
            # 政策相关
            '政策收紧', '监管加强', '反垄断', '限制', '禁令',
            '制裁', '关税', '贸易摩擦', '退市', 'ST', '*ST',
            
            # 市场相关
            '资金流出', '机构减仓', '北向资金流出', '外资减持',
            '评级下调', '目标价下调', '减持', '清仓', '割肉',
            
            # 技术面
            '跌破支撑', '死叉', '空头排列', '趋势向下', '缩量下跌'
        }
        
        # ========== 行业特定词库 ==========
        self.industry_words = {
            '半导体': {
                'positive': ['芯片', '晶圆', '光刻', '封测', '国产化', '自主可控', '产能紧张', '涨价'],
                'negative': ['制裁', '限制', '出口管制', '技术封锁', '产能过剩']
            },
            '新能源': {
                'positive': ['锂电', '光伏', '风电', '储能', '碳中和', '新能源车', '充电桩', '绿电'],
                'negative': ['补贴退坡', '产能过剩', '价格战', '技术路线', '安全事故']
            },
            '医药': {
                'positive': ['创新药', '临床试验', '获批', '医保谈判', '集采中标', '专利', 'license-out'],
                'negative': ['集采', '降价', '仿制药', '研发失败', '副作用', '召回']
            },
            '消费': {
                'positive': ['消费升级', '品牌力', '渠道扩张', '门店增长', '提价', '需求旺盛'],
                'negative': ['消费降级', '库存积压', '需求疲软', '竞争激烈', '关店']
            },
            '金融': {
                'positive': ['降息', '降准', '流动性宽松', '资产质量', '净息差', '手续费'],
                'negative': ['加息', '坏账', '不良率', '资本充足率', '监管处罚']
            },
            '房地产': {
                'positive': ['政策放松', '限购取消', '销售回暖', '去库存', '土地出让'],
                'negative': ['暴雷', '烂尾', '资金链断裂', '降价促销', '限购']
            },
            '科技': {
                'positive': ['人工智能', 'AI', '大模型', '算力', '数据要素', '数字经济'],
                'negative': ['监管', '数据安全', '隐私', '裁员', '业务收缩']
            }
        }
        
        # ========== 强度词 ==========
        self.intensifiers = {
            '大幅': 1.5, '显著': 1.4, '明显': 1.3, '急剧': 1.6,
            '暴': 1.8, '猛': 1.7, '巨': 1.8, '超': 1.5,
            '极': 1.6, '非常': 1.4, '特别': 1.3, '尤其': 1.3,
            '严重': 1.5, '重大': 1.4, '剧烈': 1.6, '强劲': 1.4,
            '快速': 1.3, '持续': 1.2, '稳步': 1.1, '逐步': 1.0
        }
        
        # ========== 否定词 ==========
        self.negations = {
            '不', '没', '未', '无', '非', '否', '别', '莫', '勿',
            '没有', '不是', '未曾', '并非', '绝非', '从未'
        }
        
        # ========== 情感词典（用于SnowNLP补充） ==========
        self.custom_words = list(self.positive_words | self.negative_words)
    
    def analyze(self, text: str, use_nlp: bool = True) -> Dict:
        """
        分析文本情感
        
        Args:
            text: 文本
            use_nlp: 是否使用NLP模型
        
        Returns:
            Dict: 情感分析结果
        """
        if not text:
            return {
                'score': 0,
                'label': 'neutral',
                'confidence': 0,
                'positive_count': 0,
                'negative_count': 0,
                'method': 'empty'
            }
        
        # 方法1: 关键词匹配
        keyword_result = self._keyword_analysis(text)
        
        # 方法2: SnowNLP（如果可用）
        nlp_result = None
        if use_nlp and HAS_SNOWNLP:
            nlp_result = self._nlp_analysis(text)
        
        # 方法3: 行业特定分析
        industry_result = self._industry_analysis(text)
        
        # 综合结果
        if nlp_result is not None:
            # 结合关键词和NLP结果
            final_score = (keyword_result['score'] * 0.5 + 
                          nlp_result['score'] * 0.3 + 
                          industry_result['score'] * 0.2)
            method = 'combined'
        else:
            # 仅关键词和行业分析
            final_score = keyword_result['score'] * 0.7 + industry_result['score'] * 0.3
            method = 'keyword'
        
        # 确定标签
        if final_score > 0.15:
            label = 'positive'
        elif final_score < -0.15:
            label = 'negative'
        else:
            label = 'neutral'
        
        # 计算置信度
        confidence = min(abs(final_score) * 1.5, 1.0)
        
        return {
            'score': round(final_score, 3),
            'label': label,
            'confidence': round(confidence, 3),
            'positive_count': keyword_result['positive_count'],
            'negative_count': keyword_result['negative_count'],
            'positive_words': keyword_result['positive_words'],
            'negative_words': keyword_result['negative_words'],
            'industry': industry_result.get('industry', ''),
            'industry_score': industry_result.get('score', 0),
            'nlp_score': nlp_result['score'] if nlp_result else None,
            'method': method
        }
    
    def _keyword_analysis(self, text: str) -> Dict:
        """关键词分析"""
        positive_count = 0
        negative_count = 0
        positive_words_found = []
        negative_words_found = []
        
        # 使用jieba分词（如果可用）
        if HAS_JIEBA:
            words = list(jieba.cut(text))
        else:
            words = self._simple_tokenize(text)
        
        for i, word in enumerate(words):
            # 检查否定
            is_negated = False
            if i > 0:
                context = ''.join(words[max(0, i-3):i])
                for neg in self.negations:
                    if neg in context:
                        is_negated = True
                        break
            
            # 检查强度
            intensity = 1.0
            if i > 0:
                context = ''.join(words[max(0, i-2):i])
                for intensifier, mult in self.intensifiers.items():
                    if intensifier in context:
                        intensity = mult
                        break
            
            # 检查正面词
            if word in self.positive_words:
                if is_negated:
                    negative_count += intensity
                    negative_words_found.append(f"不{word}")
                else:
                    positive_count += intensity
                    positive_words_found.append(word)
            
            # 检查负面词
            elif word in self.negative_words:
                if is_negated:
                    positive_count += intensity * 0.5
                    positive_words_found.append(f"不{word}")
                else:
                    negative_count += intensity
                    negative_words_found.append(word)
        
        # 计算分数
        total = positive_count + negative_count
        score = (positive_count - negative_count) / total if total > 0 else 0
        
        return {
            'score': score,
            'positive_count': positive_count,
            'negative_count': negative_count,
            'positive_words': positive_words_found,
            'negative_words': negative_words_found
        }
    
    def _nlp_analysis(self, text: str) -> Dict:
        """SnowNLP分析"""
        try:
            s = SnowNLP(text)
            score = (s.sentiments - 0.5) * 2  # 转换到 -1 到 1 范围
            return {'score': score}
        except Exception:
            return {'score': 0}
    
    def _industry_analysis(self, text: str) -> Dict:
        """行业特定分析"""
        scores = []
        detected_industry = ''
        
        for industry, words in self.industry_words.items():
            # 检测是否涉及该行业
            if any(w in text for w in words['positive'] + words['negative']):
                pos_count = sum(1 for w in words['positive'] if w in text)
                neg_count = sum(1 for w in words['negative'] if w in text)
                
                total = pos_count + neg_count
                if total > 0:
                    score = (pos_count - neg_count) / total
                    scores.append((industry, score, total))
        
        if scores:
            # 选择提及最多的行业
            scores.sort(key=lambda x: x[2], reverse=True)
            detected_industry = scores[0][0]
            avg_score = sum(s[1] for s in scores) / len(scores)
            return {'industry': detected_industry, 'score': avg_score}
        
        return {'industry': '', 'score': 0}
    
    def _simple_tokenize(self, text: str) -> List[str]:
        """简单分词（无jieba时使用）"""
        # 清理文本
        text = re.sub(r'[^\u4e00-\u9fa5a-zA-Z0-9]', ' ', text)
        
        words = []
        # 中文词（2-4字组合）
        chinese_text = re.sub(r'[^\u4e00-\u9fa5]', '', text)
        for length in [4, 3, 2]:
            for i in range(len(chinese_text) - length + 1):
                word = chinese_text[i:i+length]
                words.append(word)
        
        # 英文词
        english_words = re.findall(r'[a-zA-Z]+', text)
        words.extend(english_words)
        
        return words
    
    def analyze_news(self, news: Dict) -> Dict:
        """分析新闻情感"""
        title = news.get('title', '')
        content = news.get('content', '')
        source = news.get('source', '')
        
        # 标题权重更高
        text = f"{title} {title} {content}"
        
        # 分析情感
        result = self.analyze(text)
        
        # 计算重要性
        importance = self._calculate_importance(news, result)
        
        result['importance'] = importance
        result['title'] = title
        result['source'] = source
        
        return result
    
    def _calculate_importance(self, news: Dict, sentiment: Dict) -> int:
        """计算新闻重要性 (0-10)"""
        importance = 5
        
        title = news.get('title', '')
        content = news.get('content', '')
        source = news.get('source', '')
        
        # 来源权重
        source_weights = {
            '财联社': 2, '东方财富': 1, '新浪财经': 1,
            '证券时报': 1, '上海证券报': 1, '中国证券报': 1
        }
        importance += source_weights.get(source, 0)
        
        # 标题关键词
        high_importance_keywords = [
            '重大', '紧急', '突发', '重磅', '央行', '国务院',
            '证监会', '降息', '降准', '加息', '政策', '暴雷',
            '涨停', '跌停', '熔断'
        ]
        for keyword in high_importance_keywords:
            if keyword in title:
                importance += 1
                break
        
        # 情感强度
        if abs(sentiment['score']) > 0.5:
            importance += 1
        
        # 内容长度
        if len(content) > 500:
            importance += 1
        
        return min(importance, 10)
    
    def extract_keywords(self, text: str, top_k: int = 10) -> List[str]:
        """提取关键词"""
        if HAS_JIEBA:
            return jieba.analyse.extract_tags(text, topK=top_k)
        else:
            # 简单提取
            words = self._simple_tokenize(text)
            word_counts = defaultdict(int)
            for w in words:
                if len(w) >= 2:
                    word_counts[w] += 1
            return sorted(word_counts.keys(), key=lambda x: word_counts[x], reverse=True)[:top_k]
    
    def batch_analyze(self, news_list: List[Dict]) -> List[Dict]:
        """批量分析新闻"""
        return [self.analyze_news(news) for news in news_list]
    
    def get_sentiment_summary(self, news_list: List[Dict]) -> Dict:
        """获取情感摘要"""
        if not news_list:
            return {'total': 0, 'positive': 0, 'negative': 0, 'neutral': 0, 'avg_score': 0}
        
        results = self.batch_analyze(news_list)
        
        positive = sum(1 for r in results if r['label'] == 'positive')
        negative = sum(1 for r in results if r['label'] == 'negative')
        neutral = sum(1 for r in results if r['label'] == 'neutral')
        avg_score = sum(r['score'] for r in results) / len(results)
        
        return {
            'total': len(news_list),
            'positive': positive,
            'negative': negative,
            'neutral': neutral,
            'avg_score': round(avg_score, 3),
            'sentiment_label': 'positive' if avg_score > 0.1 else ('negative' if avg_score < -0.1 else 'neutral')
        }