from flask import Flask, request, jsonify,render_template
import time
import requests
app = Flask(__name__)
import time

def make_api_request(url, token, max_retries=3):
    """Helper function to make API requests with retry logic"""
    headers = {
        'Authorization': f'Bearer {token}',
        'accept': 'application/json'
    }
    
    for attempt in range(max_retries):
        try:
            response = requests.get(url, headers=headers, timeout=10)
            if response.status_code == 200:
                return response.json()
            else:
                return {'error': 404}  # Replace long error string with number
        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                time.sleep(1)
                continue
            # return {'error': 'Request timeout after retries'}
            return {'error': '405'}
        except requests.exceptions.ConnectionError:
            if attempt < max_retries - 1:
                time.sleep(1)
                continue
            # return {'error': 'Connection error after retries'}
            return {'error': '406'}
        except Exception as e:
            if attempt < max_retries - 1:
                time.sleep(1)
                continue
            return {'error': str(e)}
    
    # return {'error': 'Failed to fetch after maximum retries'}
    return {'error': '406'}


def get_normal_details(movie_id, token):
    """Fetch basic movie details"""
    try:
        url = f'https://api.themoviedb.org/3/movie/{movie_id}?language=en-US'
        result = make_api_request(url, token)
        return {'normal_details': result}
    except Exception as e:
        return {'normal_details': {'error': str(e)}}
    
    # poster_path : https://image.tmdb.org/t/p/w300_and_h450_face/hMRIyBjPzxaSXWM06se3OcNjIQa.jpg
    # backdrop_path : https://image.tmdb.org/t/p/w780/prH7Lmo7V9GuMbhCaDCSa6kvZvs.jpg


def get_alternative_titles(movie_id, token):
    """Fetch alternative titles"""
    try:
        url = f'https://api.themoviedb.org/3/movie/{movie_id}/alternative_titles'
        result = make_api_request(url, token)
        return {'alternative_titles': result}
    except Exception as e:
        return {'alternative_titles': {'error': str(e)}}


def get_credits(movie_id, token):
    """Fetch movie credits (cast and crew)"""
    try:
        url = f'https://api.themoviedb.org/3/movie/{movie_id}/credits?language=en-US'
        result = make_api_request(url, token)
        return {'credits': result}
    except Exception as e:
        return {'credits': {'error': str(e)}}


def get_images(movie_id, token):
    """Fetch movie images"""
    try:
        url = f'https://api.themoviedb.org/3/movie/{movie_id}/images'
        result = make_api_request(url, token)
        return {'images': result}
    except Exception as e:
        return {'images': {'error': str(e)}}


def get_keywords(movie_id, token):
    """Fetch movie keywords"""
    try:
        url = f'https://api.themoviedb.org/3/movie/{movie_id}/keywords'
        result = make_api_request(url, token)
        return {'keywords': result}
    except Exception as e:
        return {'keywords': {'error': str(e)}}


def get_videos(movie_id, token):
    """Fetch movie videos"""
    try:
        url = f'https://api.themoviedb.org/3/movie/{movie_id}/videos?language=en-US'
        result = make_api_request(url, token)
        return {'videos': result}
    except Exception as e:
        return {'videos': {'error': str(e)}}


@app.route('/scrape', methods=['POST'])
def scrape():
    """Main endpoint that combines all movie details"""
    movie_id = request.json.get('movie_id')
    token = request.json.get('token')
    
    if not movie_id:
        return jsonify({'error': 'No movie_id provided'}), 400
    if not token:
        return jsonify({'error': 'No token (TMDB_API_TOKEN) provided'}), 400
    
    try:
        # Initialize all result keys to ensure no missing fields
        result = {
            'normal_details': {},
            # 'alternative_titles': {},
            # 'credits': {},
            # 'images': {},
            # 'keywords': {},
            # 'videos': {}
        }

        # Call all functions
        result.update(get_normal_details(movie_id, token))
        # result.update(get_alternative_titles(movie_id, token))
        # result.update(get_credits(movie_id, token))
        # result.update(get_images(movie_id, token))
        # result.update(get_keywords(movie_id, token))
        # result.update(get_videos(movie_id, token))
        
        return jsonify(result)
    
    except Exception as e:
        return jsonify({'error': str(e)}), 500



@app.route('/ping', methods=['GET'])
def ping():
    return "chamkila chetan!", 200
@app.route('/success')
def home():
    return render_template('index.html')
if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)
