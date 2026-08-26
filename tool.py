"""Tools and utilities for the Discord payment bot."""

import os
import asyncio
from typing import Any
import aiohttp


# ==================== DISCORD ACCOUNT TOKEN CHECKER ====================

async def parse_token_input(input_string: str) -> str | None:
    """
    Parse different token input formats:
    - Direct token: "token_here"
    - EMAIL:PASSWORD:TOKEN format: "email@gmail.com:password123:token_here"
    
    Returns the extracted token or None if invalid format
    """
    if not input_string or not input_string.strip():
        return None
    
    # Check if it's EMAIL:PASSWORD:TOKEN format
    if input_string.count(':') == 2:
        parts = input_string.split(':')
        email, password, token = parts[0].strip(), parts[1].strip(), parts[2].strip()
        if email and password and token:
            return token
    
    # Otherwise treat as direct token
    return input_string.strip() if input_string.strip() else None


async def get_nitro_status(user_data: dict) -> dict[str, Any]:
    """
    Determine nitro status from user data
    
    Returns:
        {
            'has_nitro': bool,
            'nitro_type': str ('none', 'nitro_classic', 'nitro'),
            'boost_status': str or None
        }
    """
    premium_type = user_data.get('premium_type', 0)
    
    nitro_map = {
        0: 'none',
        1: 'nitro_classic',
        2: 'nitro'
    }
    
    nitro_type = nitro_map.get(premium_type, 'none')
    
    return {
        'has_nitro': premium_type > 0,
        'nitro_type': nitro_type,
        'nitro_since': user_data.get('premium_since')  # When they subscribed
    }


async def get_guild_count(token: str) -> dict[str, Any]:
    """Get number of servers the token is in"""
    try:
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": token}
            async with session.get(
                "https://discord.com/api/v10/users/@me/guilds",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status == 200:
                    guilds = await response.json()
                    return {
                        'success': True,
                        'guild_count': len(guilds),
                        'guilds': [{'id': g.get('id'), 'name': g.get('name'), 'owner': g.get('owner')} for g in guilds]
                    }
                else:
                    return {'success': False, 'guild_count': 0, 'error': f'Status {response.status}'}
    except Exception as e:
        return {'success': False, 'guild_count': 0, 'error': str(e)}


async def validate_discord_token(token: str) -> dict[str, Any]:
    """
    Comprehensive Discord account token validation
    
    Returns all user details:
    {
        'valid': bool,
        'user_id': str,
        'username': str,
        'email': str,
        'verified': bool,
        'mfa_enabled': bool,
        'avatar': str,
        'avatar_url': str,
        'guild_count': int,
        'guild_list': list,
        'has_nitro': bool,
        'nitro_type': str,
        'nitro_since': str,
        'premium_type': int,
        'public_flags': int,
        'accent_color': str,
        'banner': str,
        'error': str (if invalid)
    }
    """
    try:
        async with aiohttp.ClientSession() as session:
            headers = {"Authorization": token}
            
            # Get user info
            async with session.get(
                "https://discord.com/api/v10/users/@me",
                headers=headers,
                timeout=aiohttp.ClientTimeout(total=10)
            ) as response:
                if response.status == 200:
                    user_data = await response.json()
                    
                    # Get nitro status
                    nitro_info = await get_nitro_status(user_data)
                    
                    # Get guild count
                    guild_info = await get_guild_count(token)
                    
                    # Build avatar URL
                    avatar_url = f"https://cdn.discordapp.com/avatars/{user_data.get('id')}/{user_data.get('avatar')}.png"
                    
                    return {
                        'valid': True,
                        'user_id': user_data.get('id'),
                        'username': user_data.get('username'),
                        'discriminator': user_data.get('discriminator'),
                        'email': user_data.get('email'),
                        'verified': user_data.get('verified'),
                        'mfa_enabled': user_data.get('mfa_enabled'),
                        'avatar': user_data.get('avatar'),
                        'avatar_url': avatar_url,
                        'guild_count': guild_info.get('guild_count', 0),
                        'guild_list': guild_info.get('guilds', []),
                        'has_nitro': nitro_info['has_nitro'],
                        'nitro_type': nitro_info['nitro_type'],
                        'nitro_since': nitro_info.get('nitro_since'),
                        'premium_type': user_data.get('premium_type'),
                        'public_flags': user_data.get('public_flags'),
                        'accent_color': user_data.get('accent_color'),
                        'banner': user_data.get('banner'),
                        'system': user_data.get('system'),
                        'bot': user_data.get('bot'),
                        'locale': user_data.get('locale')
                    }
                    
                elif response.status == 401:
                    return {'valid': False, 'error': 'Invalid or expired token'}
                elif response.status == 429:
                    return {'valid': False, 'error': 'Rate limited - try again later'}
                else:
                    return {'valid': False, 'error': f'API error: {response.status}'}
                    
    except asyncio.TimeoutError:
        return {'valid': False, 'error': 'Request timeout - Discord API not responding'}
    except Exception as e:
        return {'valid': False, 'error': f'Error: {str(e)}'}


async def check_token_format(input_string: str) -> dict[str, Any]:
    """
    Check and parse token input, then validate
    Supports: direct token or EMAIL:PASSWORD:TOKEN format
    """
    token = await parse_token_input(input_string)
    
    if not token:
        return {'valid': False, 'error': 'Invalid input format'}
    
    return await validate_discord_token(token)


async def get_quick_token_status(input_string: str) -> bool:
    """Quick True/False check if token is valid"""
    result = await check_token_format(input_string)
    return result.get('valid', False)
