import pandas as pd
import numpy as np
import nltk
from nltk.corpus import stopwords
from nltk.stem import WordNetLemmatizer
import re


def clean_text_data(text_data, custom_stopwords=[]):
    lemmatizer = WordNetLemmatizer()
    text = " ".join(text_data)

    text = re.sub(r"\[[0-9]*\]", " ", text)
    text = re.sub(r"\s+", " ", text)
    text = re.sub("[^a-zA-Z]", " ", text)
    text = re.sub(r"\s+", " ", text)

    text = text.lower()

    text = nltk.word_tokenize(text)
    text = [lemmatizer.lemmatize(word) for word in text]

    text = [word for word in text if word not in stopwords.words("english")]
    if len(custom_stopwords) > 0:
        text = [word for word in text if word not in custom_stopwords]

    text = " ".join(text)

    return text


from sklearn.feature_extraction.text import CountVectorizer
from sklearn.decomposition import LatentDirichletAllocation


def create_topic_model(text, num_topics=10, n_words=7):

    tokens = text.split()

    # Initialize CountVectorizer
    vectorizer = CountVectorizer(
        min_df=5,
        max_df=0.9,
        stop_words="english",
        lowercase=True,
        token_pattern=r"[a-zA-Z\-][a-zA-Z\-]{2,}",
    )
    data_vectorized = vectorizer.fit_transform(tokens)

    # Build LDA Model
    lda_model = LatentDirichletAllocation(
        n_components=num_topics, max_iter=10, learning_method="online"
    )
    lda_Z = lda_model.fit_transform(data_vectorized)

    # Show the top n keywords for each topic
    def show_topics(vectorizer=vectorizer, lda_model=lda_model, n_words=20):
        keywords = np.array(vectorizer.get_feature_names_out())
        topic_keywords = []
        for topic_weights in lda_model.components_:
            top_keyword_locs = (-topic_weights).argsort()[:n_words]
            topic_keywords.append(keywords.take(top_keyword_locs))
        return topic_keywords

    topic_keywords = show_topics(
        vectorizer=vectorizer, lda_model=lda_model, n_words=n_words
    )

    # Topic - Keywords Dataframe
    df_topic_keywords = pd.DataFrame(topic_keywords)
    df_topic_keywords.columns = [
        "Word " + str(i) for i in range(df_topic_keywords.shape[1])
    ]
    return df_topic_keywords


# create_topic_model(sentence, num_topics=4, n_words=9)
