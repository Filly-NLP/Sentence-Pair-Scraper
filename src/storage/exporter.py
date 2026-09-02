import os
import json
import csv
from datetime import datetime, time, timedelta
from typing import Optional
from sqlalchemy.orm import Session
from src.storage.models import Sentence, Article, Source

class Exporter:
    def __init__(self, db_session: Session):
        self.session = db_session

    @staticmethod
    def _apply_date_filters(query, from_date: Optional[datetime], to_date: Optional[datetime]):
        """Apply inclusive date-only bounds without relying on 23:59:59."""
        if from_date:
            query = query.filter(Article.publication_date >= from_date)
        if to_date:
            # The CLI parses YYYY-MM-DD as midnight. Treat that value as the
            # named day and use an exclusive next-day bound, preserving rows
            # published later on the same day.
            if to_date.time() == time.min:
                query = query.filter(Article.publication_date < to_date + timedelta(days=1))
            else:
                query = query.filter(Article.publication_date <= to_date)
        return query

    def export_to_jsonl(
        self,
        output_path: str,
        source_id: Optional[str] = None,
        from_date: Optional[datetime] = None,
        to_date: Optional[datetime] = None,
    ) -> int:
        """Export clean unique sentences with optional date filters to JSONL."""
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        
        query = (
            self.session.query(Sentence, Article, Source)
            .join(Article, Sentence.article_id == Article.article_id)
            .join(Source, Sentence.source_id == Source.source_id)
            .filter(Sentence.language == "FILIPINO")
            .filter(Sentence.is_duplicate == False)
        )
        
        if source_id:
            query = query.filter(Sentence.source_id == source_id)
        query = self._apply_date_filters(query, from_date, to_date)

        count = 0
        with open(output_path, "w", encoding="utf-8") as f:
            for sent, art, src in query.yield_per(1000):
                record = {
                    "sentence_id": sent.sentence_id,
                    "sentence": sent.sentence_text,
                    "source": src.name,
                    "article_id": art.article_id,
                    "publication_date": art.publication_date.strftime("%Y-%m-%d") if art.publication_date else None,
                    "date_source": art.date_source,
                    "url": art.url,
                    "canonical_url": art.canonical_url,
                    "language": sent.language,
                    "language_confidence": sent.language_confidence
                }
                f.write(json.dumps(record, ensure_ascii=False) + "\n")
                count += 1
                
        return count

    def export_to_csv(self, output_path: str, source_id: Optional[str] = None, 
                      from_date: Optional[datetime] = None, to_date: Optional[datetime] = None, 
                      append: bool = False) -> int:
        """Export clean unique sentences with full provenance to CSV format with optional date range filters and append mode."""
        os.makedirs(os.path.dirname(os.path.abspath(output_path)), exist_ok=True)
        
        query = (
            self.session.query(Sentence, Article, Source)
            .join(Article, Sentence.article_id == Article.article_id)
            .join(Source, Sentence.source_id == Source.source_id)
            .filter(Sentence.language == "FILIPINO")
            .filter(Sentence.is_duplicate == False)
        )
        
        if source_id:
            query = query.filter(Sentence.source_id == source_id)
            
        query = self._apply_date_filters(query, from_date, to_date)

        file_exists = os.path.exists(output_path)
        write_mode = "a" if (append and file_exists) else "w"
        
        count = 0
        with open(output_path, write_mode, newline="", encoding="utf-8") as f:
            writer = csv.writer(f)
            
            # Write header if not in append mode, or if the file does not exist
            if not (append and file_exists):
                writer.writerow([
                    "sentence_id", "sentence", "normalized_sentence", "source_name", "source_id", 
                    "article_id", "headline", "author", "publication_date", "url", 
                    "language", "language_confidence", "token_count", "quality_score", 
                    "paragraph_index", "sentence_index", "is_quote", "date_source", "canonical_url"
                ])
            
            for sent, art, src in query.yield_per(1000):
                writer.writerow([
                    sent.sentence_id,
                    sent.sentence_text,
                    sent.normalized_text,
                    src.name,
                    sent.source_id,
                    art.article_id,
                    art.headline or "",
                    art.author or "",
                    art.publication_date.strftime("%Y-%m-%d") if art.publication_date else "",
                    art.url,
                    sent.language,
                    sent.language_confidence,
                    sent.token_count,
                    sent.quality_score,
                    sent.paragraph_index,
                    sent.sentence_index,
                    1 if sent.is_quote else 0,
                    art.date_source or "",
                    art.canonical_url or "",
                ])
                count += 1
                
        return count
