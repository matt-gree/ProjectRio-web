from flask import request, jsonify, abort
from flask import current_app as app
from flask_jwt_extended import jwt_required, get_jwt_identity
from flask_jwt_extended import create_access_token, set_access_cookies, jwt_required, get_jwt_identity, get_jwt, unset_jwt_cookies
from app.utils.send_email import send_email
import secrets
from ..models import *
from ..consts import *
import time

@app.route('/tag/create', methods=['POST'])
@jwt_required(optional=True)
def tag_create():
    in_tag_name = request.json['Tag Name']
    in_tag_desc = request.json['Description']
    in_tag_comm_name = request.json['Community Name']
    in_tag_type = request.json['Tag Type']

    #Fields for gecko codes only
    code_desc_provided = request.is_json and 'Code Desc' in request.json
    code_desc = request.json.get('Code Desc') if code_desc_provided else None

    code_provided = request.is_json and 'Code' in request.json
    code = request.json.get('Code') if code_provided else None

    comm_name_lower = in_tag_comm_name.lower()
    comm = Community.query.filter_by(name_lowercase=comm_name_lower).first()

    creating_gecko_code = (in_tag_type == "Code" and (not code_desc_provided or not code_provided))

    if comm == None:
        return abort(409, description="No community found with name={in_tag_comm_name}")
    if in_tag_type not in cTAG_TYPES.values() or in_tag_type == "Competition" or in_tag_type == "Community":
        return abort(410, description="Invalid tag type '{in_tag_type}'")
    if ((in_tag_type == "Code" or in_tag_type == "Client Code") and not comm.official):
        return abort(411, description="Type is gecko code but code details not provided")
    if (in_tag_type == "Code" and (not code_desc_provided or not code_provided)):
        return abort(412, description="Type is gecko code but code details not provided")

    # Get user making the new community
    #Get user via JWT or RioKey
    user=None
    current_user_username = get_jwt_identity()
    if current_user_username:
        user = RioUser.query.filter_by(username=current_user_username).first()
    else:
        try:
            user = RioUser.query.filter_by(rio_key=request.json['Rio Key']).first()
        except:
            return abort(409, description="No Rio Key or JWT Provided")

    if user == None:
        return abort(409, description='Username associated with JWT not found.')
    
    #If community tag, make sure user is an admin of the community
    comm_user = CommunityUser.query.filter_by(user_id=user.id, community_id=comm.id).first()

    if (comm_user == None or comm_user.admin == False):
        return abort(409, description='User not a part of community or not an admin')

    # === Tag Creation ===
    new_tag = Tag( in_comm_id=comm.id, in_tag_name=in_tag_name, in_tag_type=in_tag_type, in_desc=in_tag_desc)
    db.session.add(new_tag)
    db.session.commit()

    # === Code Tag Creation ===
    if (creating_gecko_code):
        new_code_tag = CodeTag(in_tag_id=new_tag.id, in_code_desc=code_desc, in_code=code)
        db.session.add(new_code_tag)
        db.session.commit()
    
    return jsonify(new_tag.name)

@app.route('/tag/list', methods=['GET'])
def tag_list():
    client = request.is_json and 'Client' in request.json

    types_provided = request.is_json and 'Types' in request.json
    types_list = request.json.get('Types') if types_provided else list()

    # Abort if any of the provided types are not valid
    if (types_provided and not any(x in types_list for x in cTAG_TYPES.values())):
        return abort(409, description=f"Illegal type name provided. Valid types {cTAG_TYPES.values()}")

    communities_provided = request.is_json and 'Communities' in request.json
    community_id_list = request.json.get('Communities') if communities_provided else list() 

    result = list()
    if types_provided and not communities_provided:
        result = Tag.query.filter(Tag.tag_type.in_(types_list))
    elif not types_provided and communities_provided:
        result = Tag.query.join(Community, Tag.community_id == Community.id)\
            .filter(Community.id.in_(community_id_list))
    elif types_provided and communities_provided:
        result = Tag.query.join(Community, Tag.community_id == Community.id)\
            .filter((Community.id.in_(community_id_list)) & (Tag.tag_type.in_(types_list)))
    else:
        result = Tag.query.all()

    #IF CALLED BY CLIENT THE FOLLOWING COMMENT APPLIES
    #The return type of this function is a list of tag dicts. The tag dicts contain additional
    #fields from the CodeTag table even if the Tag does not have an associated CodeTag. In that
    #case the two CodeTag values are empty strings. This is to make life easier for the client c++
    #code to parse
    tags = list()
    for tag in result:
        final_tag_dict = tag.to_dict()
        result = CodeTag.query.filter_by(tag_id=tag.id).first()
        if (result != None):
            code_dict = result.to_dict()
        elif client:
            code_dict = {
                "code_desc": "",
                "code": ""
            }
        else:
            code_dict = dict()
        tags.append(final_tag_dict.update(code_dict))
    return { 'Tags': tags }

#TODO support duration along with end data so eiither can be supplied
@app.route('/tag_set/create', methods=['POST'])
@jwt_required(optional=True)
def tagset_create():
    in_tag_set_name = request.json['TagSet Name']
    in_tag_set_desc = request.json['Description']
    in_tag_set_type = request.json['Type']
    in_tag_set_comm_name = request.json['Community Name']
    in_tag_ids = request.json['Tags']
    in_tag_set_start_time = request.json['Start']
    in_tag_set_end_time = request.json['End']

    comm_name_lower = in_tag_set_comm_name.lower()
    comm = Community.query.filter_by(name_lowercase=comm_name_lower).first()

    if comm == None:
        return abort(409, description=f"No community found with name={in_tag_set_comm_name}")
    if comm.sponsor_id == None:
        return abort(410, description=f"Community is not sponsored")
    if in_tag_set_name.isalnum() == False:
        return abort(406, description='Provided tag set name is not alphanumeric. Community not created')
    if in_tag_set_end_time < in_tag_set_start_time:
        return abort(409, description='Invalid start/end times')
    if in_tag_set_type not in cTAG_SET_TYPES.values():
        return abort(409, description='Invalid tag type')

    # Make sure community is under the limit of active tag types
    current_unix_time = int( time.time() )

    query = (
        'SELECT \n'
        'MAX(community.active_tag_set_limit) AS tag_set_limit, \n'
        'COUNT(*) AS active_tag_sets \n'
        'FROM tag_set \n'
        'JOIN community ON community.id = tag_set.community_id \n'
       f'WHERE tag_set.end_date > {current_unix_time} \n'
        'GROUP BY tag_set.community_id \n'
    )
    results = db.session.execute(query).first()
    if results != None:
        result_dict = results._asdict()
        print(result_dict)
        if result_dict['active_tag_sets'] >= result_dict['tag_set_limit']:
            return abort(413, description='Community has reached active tag_set_limit')

    # Get user making the new community
    #Get user via JWT or RioKey
    user=None
    current_user_username = get_jwt_identity()
    if current_user_username:
        user = RioUser.query.filter_by(username=current_user_username).first()
    else:
        try:
            user = RioUser.query.filter_by(rio_key=request.json['Rio Key']).first()
        except:
            return abort(409, description="No Rio Key or JWT Provided")

    if user == None:
        return abort(409, description='Username associated with JWT not found.')
    
    #If community tag, make sure user is an admin of the community
    comm_user = CommunityUser.query.filter_by(user_id=user.id, community_id=comm.id).first()

    if (comm_user == None or comm_user.admin == False):
        return abort(409, description='User not apart of community or not an admin')

    # Validate all tag ids, add to list
    tags = list()
    for id in in_tag_ids:
        tag = Tag.query.filter_by(id=id).first()
        if tag == None:
            return abort(409, f'Tag with ID={id} not found')
        if tag.tag_type != "Component":
            return abort(409, f'Tag with ID={id} not a component tag')
        tags.append(tag)

    # === Tag Set Creation ===
    new_tag_set = TagSet(in_comm_id=comm.id, in_name=in_tag_set_name,in_type=in_tag_set_type, in_start=in_tag_set_start_time, in_end=in_tag_set_end_time)
    db.session.add(new_tag_set)
    db.session.commit()

    # === Tag Creation ===
    new_tag_set_tag = Tag( in_comm_id=comm.id, in_tag_name=in_tag_set_name, in_tag_type="Competition", in_desc=in_tag_set_desc)
    db.session.add(new_tag_set_tag)
    db.session.commit()
    tags.append(new_tag_set_tag)

    # TagSetTags
    # Get Comm tag
    comm_tag = Tag.query.filter_by(community_id=comm.id, tag_type="Community").first()
    if comm_tag == None:
        return abort(409, description='Could not find community tag for community')
    tags.append(comm_tag)

    for tag in tags:
        new_tag_set.tags.append(tag)
    
    db.session.commit()
    return jsonify(new_tag_set.to_dict())

# If RioKey/JWT provided get TagSet for user. Else get all
# Uses:
#   Get all active TagSets for rio_key
#   Get all active and inactive TagSets for rio_key
#   Get all active/inactive TagSets for provided communities per RioKey
@app.route('/tag_set/list', methods=['POST'])
def tagset_list():    
    current_unix_time = int( time.time() )
    active_only = request.is_json and 'Active' in request.json and request.json['Active'].lower() in ['yes', 'y', 'true', 't']
    communities_provided = request.is_json and 'Communities' in request.json
    community_id_list = request.json.get('Communities') if communities_provided else list()

    if (communities_provided and len(community_id_list) == 0):
        return abort(409, description="Communities key added to JSON but no community ids passed")
    
    rio_key_provided = request.is_json and 'Rio Key' in request.json
    if rio_key_provided:
        rio_key = request.json.get('Rio Key')
        tag_sets = db.session.query(
            TagSet
        ).join(
            Community
        ).join(
            CommunityUser
        ).join(
            RioUser
        ).filter(
            RioUser.rio_key == rio_key
        ).all()

        tag_set_list = list()
        for tag_set in tag_sets:
            # Skip this tag set if current time is not within start/end time
            if (active_only and (current_unix_time < tag_set.start_date or current_unix_time > tag_set.end_date)):
                continue
            # Skip this tag set if community_id is not from a requested community
            if (communities_provided and tag_set.community_id not in community_id_list):
                continue

            # Append passing tag set information
            tag_set_list.append(tag_set.to_dict())
    else:
        abort(409, "No Rio Key provided")

    return {"Tag Sets": tag_set_list}

@app.route('/tag_set/<tag_set_id>', methods=['GET'])
def tagset_get_tags(tag_set_id):
    result = TagSet.query.filter_by(id = tag_set_id).first()
    if result == None:
        return abort(409, description=f"Could not find TagSet with id={tag_set_id}")

    return {"Tag Set": [result.to_dict()]}


# @app.route('/tag_set/ladder', methods=['POST'])
# @jwt_required(optional=True)
# def community_sponsor():
#     pass
    
@app.route('/tag_set/ladder/', methods=['POST'])
@jwt_required(optional=True)
def get_ladder(in_tag_set=None):
    tag_set_name =  in_tag_set if in_tag_set != None else request.json['TagSet']
    tag_set = TagSet.query.filter_by(name_lowercase=tag_set_name.lower()).first()
    if tag_set == None:
        return abort(409, description=f"Could not find TagSet with name={tag_set_name}")

    query = (
        'SELECT \n'
        'ladder.rating, \n'
        'rio_user.id, \n'
        'rio_user.username \n'
        'FROM ladder \n'
        'JOIN community_user on community_user.id = ladder.community_user_id \n'
        'JOIN rio_user on rio_user.id = community_user.user_id \n'
       f"WHERE ladder.tag_set_id = {tag_set.id}"
    )

    results = db.session.execute(query).all()
    ladder_results = dict()
    for result_row in results:
        result_dict = result_row._asdict()
        ladder_results[result_dict['username']] = result_row._asdict()
    return jsonify(ladder_results)