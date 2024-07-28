import requests
from bs4 import BeautifulSoup

def scrape_page(url):
    # Send a GET request to the URL
    response = requests.get(url)
    
    # Check if the request was successful
    if response.status_code == 200:
        # Parse the HTML content
        soup = BeautifulSoup(response.text, 'html.parser')
        jlpt = soup.find_all('div', class_='infopanel')
        print(len(jlpt))
                

# Example usage
url = "https://www.kanshudo.com/collections/jlpt_kanji"  
scrape_page(url)
