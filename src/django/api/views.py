import os
import sys

from datetime import datetime

from django.core.files.uploadedfile import InMemoryUploadedFile
from django.db import transaction
from django.core import exceptions as core_exceptions
from django.conf import settings
from django.contrib.auth import (authenticate, login, logout)
from django.contrib.auth import password_validation
from django.contrib.auth.hashers import check_password
from rest_framework import viewsets, status
from rest_framework.authtoken.views import ObtainAuthToken
from rest_framework.authtoken.models import Token
from rest_framework.exceptions import (ValidationError,
                                       NotFound,
                                       AuthenticationFailed)
from rest_framework.generics import CreateAPIView, RetrieveUpdateAPIView
from rest_framework.decorators import (api_view,
                                       permission_classes,
                                       action)
from rest_framework.permissions import AllowAny
from rest_framework.response import Response
from rest_framework.viewsets import ReadOnlyModelViewSet
from rest_framework.filters import BaseFilterBackend
from rest_auth.views import LoginView, LogoutView
from allauth.account.models import EmailAddress
from allauth.account.utils import complete_signup
import coreapi

from oar.settings import MAX_UPLOADED_FILE_SIZE_IN_BYTES, ENVIRONMENT

from api.constants import (CsvHeaderField,
                           FacilitiesQueryParams,
                           ProcessingAction)
from api.models import (FacilityList,
                        FacilityListItem,
                        Facility,
                        FacilityMatch,
                        Contributor,
                        User)
from api.processing import parse_csv_line
from api.serializers import (FacilityListSerializer,
                             FacilityListItemSerializer,
                             FacilityQueryParamsSerializer,
                             FacilitySerializer,
                             FacilityDetailsSerializer,
                             UserSerializer,
                             UserProfileSerializer)
from api.countries import COUNTRY_CHOICES
from api.aws_batch import submit_jobs
from api.permissions import IsRegisteredAndConfirmed
from api.pagination import FacilitiesGeoJSONPagination


@permission_classes((AllowAny,))
class SubmitNewUserForm(CreateAPIView):
    serializer_class = UserSerializer

    @transaction.atomic
    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)

        if serializer.is_valid(raise_exception=True):
            serializer.save(request)
            pk = serializer.data['id']
            user = User.objects.get(pk=pk)

            name = request.data.get('name')
            description = request.data.get('description')
            website = request.data.get('website')
            contrib_type = request.data.get('contributor_type')
            other_contrib_type = request.data.get('other_contributor_type')

            if name is None:
                raise ValidationError('name cannot be blank')

            if description is None:
                raise ValidationError('description cannot be blank')

            if contrib_type is None:
                raise ValidationError('contributor type cannot be blank')

            if contrib_type == Contributor.OTHER_CONTRIB_TYPE:
                if other_contrib_type is None or len(other_contrib_type) == 0:
                    raise ValidationError(
                        'contributor type description required for Contributor'
                        ' Type \'Other\''
                    )

            Contributor.objects.create(
                admin=user,
                name=name,
                description=description,
                website=website,
                contrib_type=contrib_type,
                other_contrib_type=other_contrib_type,
            )

            complete_signup(self.request._request, user, 'optional', None)

            return Response(status=status.HTTP_204_NO_CONTENT)


class LoginToOARClient(LoginView):
    serializer_class = UserSerializer
    permission_classes = [AllowAny]

    def post(self, request, *args, **kwargs):
        email = request.data.get('email')
        password = request.data.get('password')

        if email is None or password is None:
            raise AuthenticationFailed('Email and password are required')

        user = authenticate(email=email, password=password)

        if user is None:
            raise AuthenticationFailed(
                'Unable to login with those credentials')

        login(request, user)

        if not request.user.did_register_and_confirm_email:
            # Mimic the behavior of django-allauth and resend the confirmation
            # email if an unconfirmed user tries to log in.
            email_address = EmailAddress.objects.get(email=email)
            email_address.send_confirmation(request)

            raise AuthenticationFailed(
                'Account is not verified. '
                'Check your email for a confirmation link.'
            )

        return Response(UserSerializer(user).data)

    def get(self, request, *args, **kwargs):
        if not request.user.is_active:
            raise AuthenticationFailed('Unable to sign in')
        if not request.user.did_register_and_confirm_email:
            raise AuthenticationFailed('Unable to sign in')

        return Response(UserSerializer(request.user).data)


class LogoutOfOARClient(LogoutView):
    serializer_class = UserSerializer

    def post(self, request, *args, **kwargs):
        logout(request)

        return Response(status=status.HTTP_204_NO_CONTENT)


class UserProfile(RetrieveUpdateAPIView):
    serializer_class = UserProfileSerializer

    def get(self, request, pk=None, *args, **kwargs):
        try:
            user = User.objects.get(pk=pk)
            response_data = UserProfileSerializer(user).data
            return Response(response_data)
        except User.DoesNotExist:
            raise NotFound()

    def patch(self, request, pk, *args, **kwargs):
        pass

    @permission_classes((IsRegisteredAndConfirmed,))
    def put(self, request, pk, *args, **kwargs):
        if not request.user.is_authenticated:
            raise core_exceptions.PermissionDenied

        if not request.user.did_register_and_confirm_email:
            raise core_exceptions.PermissionDenied

        try:
            user_for_update = User.objects.get(pk=pk)

            if request.user != user_for_update:
                raise core_exceptions.PermissionDenied

            current_password = request.data.get('current_password', '')
            new_password = request.data.get('new_password', '')
            confirm_new_password = request.data.get('confirm_new_password', '')

            if current_password != '' and new_password != '':
                if new_password != confirm_new_password:
                    raise ValidationError(
                        'New password and confirm new password do not match')

                if not check_password(current_password,
                                      user_for_update.password):
                    raise ValidationError('Your current password is incorrect')

                try:
                    password_validation.validate_password(
                        password=new_password, user=user_for_update)
                except core_exceptions.ValidationError as e:
                    raise ValidationError({"new_password": list(e.messages)})

            name = request.data.get('name')
            description = request.data.get('description')
            website = request.data.get('website')
            contrib_type = request.data.get('contributor_type')
            other_contrib_type = request.data.get('other_contributor_type')

            if name is None:
                raise ValidationError('name cannot be blank')

            if description is None:
                raise ValidationError('description cannot be blank')

            if contrib_type is None:
                raise ValidationError('contributor type cannot be blank')

            if contrib_type == Contributor.OTHER_CONTRIB_TYPE:
                if other_contrib_type is None or len(other_contrib_type) == 0:
                    raise ValidationError(
                        'contributor type description required for Contributor'
                        ' Type \'Other\''
                    )

            user_contributor = request.user.contributor
            user_contributor.name = name
            user_contributor.description = description
            user_contributor.website = website
            user_contributor.contrib_type = contrib_type
            user_contributor.other_contrib_type = other_contrib_type
            user_contributor.save()

            if new_password != '':
                user_for_update.set_password(new_password)

            user_for_update.save()

            # Changing the password causes the user to be logged out, which we
            # don't want
            if new_password != '':
                login(request, user_for_update)

            response_data = UserProfileSerializer(user_for_update).data
            return Response(response_data)
        except User.DoesNotExist:
            raise NotFound()
        except Contributor.DoesNotExist:
            raise NotFound()


class APIAuthToken(ObtainAuthToken):
    permission_classes = (IsRegisteredAndConfirmed,)

    def get(self, request, *args, **kwargs):
        try:
            token = Token.objects.get(user=request.user)

            token_data = {
                'token': token.key,
                'created': token.created.isoformat(),
            }

            return Response(token_data)
        except Token.DoesNotExist:
            raise NotFound()

    def post(self, request, *args, **kwargs):
        token, _ = Token.objects.get_or_create(user=request.user)

        token_data = {
            'token': token.key,
            'created': token.created.isoformat(),
        }

        return Response(token_data)

    def delete(self, request, *args, **kwargs):
        try:
            token = Token.objects.get(user=request.user)
            token.delete()

            return Response(status=status.HTTP_204_NO_CONTENT)
        except Token.DoesNotExist:
            return Response(status=status.HTTP_204_NO_CONTENT)


@api_view(['GET'])
@permission_classes((AllowAny,))
def all_contributors(request):
    """
    Returns list contributors as a list of tuples of contributor IDs and names.

    ## Sample Response

        [
            [1, "Contributor One"]
            [2, "Contributor Two"]
        ]
    """
    response_data = [
        (contributor.id, contributor.name)
        for contributor
        in Contributor.objects.filter(
            facilitylist__is_active=True,
            facilitylist__is_public=True).distinct().order_by('name')
    ]

    return Response(response_data)


@api_view(['GET'])
@permission_classes((AllowAny,))
def all_contributor_types(request):
    """
    Returns a list of contributor type choices as tuples of values and display
    names.

    ## Sample Response

        [
            ["Auditor", "Auditor"],
            ["Brand/Retailer", "Brand/Retailer"],
            ["Civil Society Organization", "Civil Society Organization"],
            ["Factory / Facility", "Factory / Facility"]
        ]
    """
    return Response(Contributor.CONTRIB_TYPE_CHOICES)


@api_view(['GET'])
@permission_classes((AllowAny,))
def all_countries(request):
    """
    Returns a list of country choices as tuples of country codes and names.

    ## Sample Response

        [
            ["AF", "Afghanistan"],
            ["AX", "Åland Islands"]
            ["AL", "Albania"]
        ]

    """
    return Response(COUNTRY_CHOICES)


class FacilitiesAPIFilterBackend(BaseFilterBackend):
    def get_schema_fields(self, view):
        if view.action == 'list':
            return [
                coreapi.Field(
                    name='name',
                    location='query',
                    type='string',
                    required=False,
                    description='Facility Name',
                ),
                coreapi.Field(
                    name='contributors',
                    location='query',
                    type='integer',
                    required=False,
                    description='Contributor ID',
                ),
                coreapi.Field(
                    name='contributor_types',
                    location='query',
                    type='string',
                    required=False,
                    description='Contributor Type',
                ),
                coreapi.Field(
                    name='countries',
                    location='query',
                    type='string',
                    required=False,
                    description='Country Code',
                ),
            ]

        return []


class FacilitiesViewSet(ReadOnlyModelViewSet):
    """
    Get facilities in GeoJSON format.
    """
    queryset = Facility.objects.all()
    serializer_class = FacilitySerializer
    permission_classes = (AllowAny,)
    pagination_class = FacilitiesGeoJSONPagination
    filter_backends = (FacilitiesAPIFilterBackend,)

    def list(self, request):
        """
        Returns a list of facilities in GeoJSON format for a given query.

        ### Sample Response
            {
                "type": "FeatureCollection",
                "features": [
                    {
                        "id": "OAR_ID_1",
                        "type": "Feature",
                        "geometry": {
                            "type": "Point",
                            "coordinates": [1, 1]
                        },
                        "properties": {
                            "name": "facility_name_1",
                            "address" "facility address_1",
                            "country_code": "US",
                            "country_name": "United States",
                            "oar_id": "OAR_ID_1"
                        }
                    },
                    {
                        "id": "OAR_ID_2",
                        "type": "Feature",
                        "geometry": {
                            "type": "Point",
                            "coordinates": [2, 2]
                        },
                        "properties": {
                            "name": "facility_name_2",
                            "address" "facility address_2",
                            "country_code": "US",
                            "country_name": "United States",
                            "oar_id": "OAR_ID_2"
                        }
                    }
                ]
            }
        """
        params = FacilityQueryParamsSerializer(data=request.query_params)

        if not params.is_valid():
            raise ValidationError(params.errors)

        name = request.query_params.get(FacilitiesQueryParams.NAME,
                                        None)
        contributors = request.query_params \
                              .getlist(FacilitiesQueryParams.CONTRIBUTORS)

        contributor_types = request \
            .query_params \
            .getlist(FacilitiesQueryParams.CONTRIBUTOR_TYPES)
        countries = request.query_params \
                           .getlist(FacilitiesQueryParams.COUNTRIES)

        queryset = Facility.objects.all()

        if name is not None:
            queryset = queryset.filter(name__icontains=name)

        if countries is not None and len(countries):
            queryset = queryset.filter(country_code__in=countries)

        if len(contributor_types):
            type_match_facility_ids = [
                match['facility__id']
                for match
                in FacilityMatch
                .objects
                .filter(status__in=[FacilityMatch.AUTOMATIC,
                                    FacilityMatch.CONFIRMED])
                .filter(facility_list_item__facility_list__contributor__contrib_type__in=contributor_types) # NOQA
                .filter(facility_list_item__facility_list__is_active=True)
                .values('facility__id')
            ]

            queryset = queryset.filter(id__in=type_match_facility_ids)

        if len(contributors):
            name_match_facility_ids = [
                match['facility__id']
                for match
                in FacilityMatch
                .objects
                .filter(status__in=[FacilityMatch.AUTOMATIC,
                                    FacilityMatch.CONFIRMED])
                .filter(facility_list_item__facility_list__contributor__id__in=contributors) # NOQA
                .filter(facility_list_item__facility_list__is_active=True)
                .values('facility__id')
            ]

            queryset = queryset.filter(id__in=name_match_facility_ids)

        page_queryset = self.paginate_queryset(queryset)

        if page_queryset is not None:
            serializer = FacilitySerializer(page_queryset, many=True)
            return self.get_paginated_response(serializer.data)

        response_data = FacilitySerializer(queryset, many=True).data
        return Response(response_data)

    def retrieve(self, request, pk=None):
        """
        Returns the facility specified by a given OAR ID in GeoJSON format.

        ### Sample Response
            {
                "id": "OAR_ID",
                "type": "Feature",
                "geometry": {
                    "type": "Point",
                    "coordinates": [1, 1]
                },
                "properties": {
                    "name": "facility_name",
                    "address" "facility address",
                    "country_code": "US",
                    "country_name": "United States",
                    "oar_id": "OAR_ID",
                    "other_names": [],
                    "other_addresses": [],
                    "contributors": [[1, "Contributor One"]]
                }
            }
        """
        try:
            queryset = Facility.objects.get(pk=pk)
            response_data = FacilityDetailsSerializer(queryset).data
            return Response(response_data)
        except Facility.DoesNotExist:
            raise NotFound()

    @action(detail=False, methods=['get'])
    def count(self, request):
        """
        Returns a count of total Facilities available in the Open Apparel
        Registry.

        ### Sample Response
            { "count": 100000 }
        """
        count = Facility.objects.count()
        return Response({"count": count})


class FacilityListViewSet(viewsets.ModelViewSet):
    """
    Upload and update facility lists for an authenticated Contributor.
    """
    queryset = FacilityList.objects.all()
    serializer_class = FacilityListSerializer
    permission_classes = [IsRegisteredAndConfirmed]
    http_method_names = ['get', 'post', 'head', 'options', 'trace']

    def _validate_header(self, header):
        if header is None or header == '':
            raise ValidationError('Header cannot be blank.')
        parsed_header = [i.lower() for i in parse_csv_line(header)]
        if CsvHeaderField.COUNTRY not in parsed_header \
           or CsvHeaderField.NAME not in parsed_header \
           or CsvHeaderField.ADDRESS not in parsed_header:
            raise ValidationError(
                'Header must contain {0}, {1}, and {2} fields.'.format(
                    CsvHeaderField.COUNTRY, CsvHeaderField.NAME,
                    CsvHeaderField.ADDRESS))

    @transaction.atomic
    def create(self, request):
        """
        Upload a new Facility List.

        ## Request Body

        *Required*

        `file` (`file`): CSV file to upload.

        *Optional*

        `name` (`string`): Name of the uploaded file.

        `description` (`string`): Description of the uploaded file.

        `replaces` (`number`): An optional ID for an existing list to replace
                   with the new list

        ### Sample Response

            {
                "id": 1,
                "name": "list name",
                "description": "list description",
                "file_name": "list-1.csv",
                "is_active": true,
                "is_public": true
            }
        """
        if 'file' not in request.data:
            raise ValidationError('No file specified.')
        csv_file = request.data['file']
        if type(csv_file) is not InMemoryUploadedFile:
            raise ValidationError('File not submitted properly.')
        if csv_file.size > MAX_UPLOADED_FILE_SIZE_IN_BYTES:
            mb = MAX_UPLOADED_FILE_SIZE_IN_BYTES / (1024*1024)
            raise ValidationError(
                'Uploaded file exceeds the maximum size of {:.1f}MB.'.format(
                    mb))
        try:
            header = csv_file.readline().decode().rstrip()
        except UnicodeDecodeError:
            ROLLBAR = getattr(settings, 'ROLLBAR', {})
            if ROLLBAR:
                import rollbar
                rollbar.report_exc_info(
                    sys.exc_info(),
                    extra_data={
                        'user_id': request.user.id,
                        'contributor_id': request.user.contributor.id,
                        'file_name': csv_file.name})
            raise ValidationError('Unsupported file encoding. Please '
                                  'submit a UTF-8 CSV.')

        self._validate_header(header)

        try:
            contributor = request.user.contributor
        except Contributor.DoesNotExist:
            raise ValidationError('User contributor cannot be None')

        if 'name' in request.data:
            name = request.data['name']
        else:
            name = os.path.splitext(csv_file.name)[0]

        if 'description' in request.data:
            description = request.data['description']
        else:
            description = None

        replaces = None
        if 'replaces' in request.data:
            try:
                replaces = int(request.data['replaces'])
            except ValueError:
                raise ValidationError('"replaces" must be an integer ID.')
            old_list_qs = FacilityList.objects.filter(
                contributor=contributor, pk=replaces)
            if old_list_qs.count() == 0:
                raise ValidationError(
                    '{0} is not a valid FacilityList ID.'.format(replaces))
            replaces = old_list_qs[0]
            if FacilityList.objects.filter(replaces=replaces).count() > 0:
                raise ValidationError(
                    'FacilityList {0} has already been replaced.'.format(
                        replaces.pk))

        new_list = FacilityList(
            contributor=contributor,
            name=name,
            description=description,
            file_name=csv_file.name,
            header=header,
            replaces=replaces)
        new_list.save()

        if replaces is not None:
            replaces.is_active = False
            replaces.save()

        items = []
        for idx, line in enumerate(csv_file):
            if idx > 0:
                try:
                    items.append(FacilityListItem(
                        row_index=(idx - 1),
                        facility_list=new_list,
                        raw_data=line.decode().rstrip()
                    ))
                except UnicodeDecodeError:
                    ROLLBAR = getattr(settings, 'ROLLBAR', {})
                    if ROLLBAR:
                        import rollbar
                        rollbar.report_exc_info(
                            sys.exc_info(),
                            extra_data={
                                'user_id': request.user.id,
                                'contributor_id': request.user.contributor.id,
                                'file_name': csv_file.name})
                    raise ValidationError('Unsupported file encoding. Please '
                                          'submit a UTF-8 CSV.')
        FacilityListItem.objects.bulk_create(items)

        if ENVIRONMENT in ('Staging', 'Production'):
            submit_jobs(ENVIRONMENT, new_list)

        serializer = self.get_serializer(new_list)
        return Response(serializer.data)

    def list(self, request):
        """
        Returns Facility Lists for an authenticated Contributor.

        ## Sample Response
            [
                {
                    "id":16,
                    "name":"11",
                    "description":"list 11",
                    "file_name":"11.csv",
                    "is_active":true,
                    "is_public":true
                },
                {
                    "id":15,
                    "name":"old list 11",
                    "description":"old list 11",
                    "file_name":"11.csv",
                    "is_active":false,
                    "is_public":true
                }
            ]
        """
        try:
            contributor = request.user.contributor
            queryset = FacilityList.objects.filter(contributor=contributor)
            response_data = self.serializer_class(queryset, many=True).data
            return Response(response_data)
        except Contributor.DoesNotExist:
            raise ValidationError('User contributor cannot be None')

    def retrieve(self, request, pk):
        """
        Returns data describing a single Facility List.

        ## Sample Response
            {
                "id": 16,
                "name": "list 11",
                "description": "list 11 description",
                "file_name": "11.csv",
                "is_active": true,
                "is_public": true,
                "item_count": 100,
                "item_url": "/api/facility-lists/16/items/"
            }
        """
        try:
            user_contributor = request.user.contributor
            facility_list = FacilityList \
                .objects \
                .filter(contributor=user_contributor) \
                .get(pk=pk)
            response_data = self.serializer_class(facility_list).data

            return Response(response_data)
        except FacilityList.DoesNotExist:
            raise NotFound()

    @action(detail=True, methods=['get'])
    def items(self, request, pk):
        """
        Returns data about a single page of Facility List Items.

        ## Sample Response
            {
                "count": 25,
                "next": "/api/facility-lists/16/items/?page=2&pageSize=20",
                "previous": null,
                "results": [
                    "id": 1,
                    "matches": [],
                    "country_name": "United States",
                    "processing_errors": null,
                    "matched_facility": null,
                    "row_index": 1,
                    "raw_data": "List item 1, List item address 1",
                    "status": "GEOCODED",
                    "processing_started_at": null,
                    "processing_completed_at": null,
                    "name": "List item 1",
                    "address": "List item address 1",
                    "country_code": "US",
                    "facility_list": 16
                ],
                ...
            }
        """
        try:
            user_contributor = request.user.contributor
            facility_list = FacilityList \
                .objects \
                .filter(contributor=user_contributor) \
                .get(pk=pk)
            queryset = FacilityListItem \
                .objects \
                .filter(facility_list=facility_list) \
                .order_by('row_index')

            page_queryset = self.paginate_queryset(queryset)
            if page_queryset is not None:
                serializer = FacilityListItemSerializer(page_queryset,
                                                        many=True)
                return self.get_paginated_response(serializer.data)

            serializer = FacilityListItemSerializer(queryset, many=True)
            return Response(serializer.data)
        except FacilityList.DoesNotExist:
            raise NotFound()

    @transaction.atomic
    @action(detail=True,
            methods=['post'],
            url_path='confirm')
    def confirm_match(self, request, pk=None):
        """
        Confirm a potential match between an existing Facility and a Facility
        List Item from an authenticated Contributor's Facility List.

        Returns an updated Facility List Item with the confirmed match's status
        changed to `CONFIRMED` and the Facility List Item's status changed to
        `CONFIRMED_MATCH`. On confirming a potential match, all other
        potential matches will have their status changed to `REJECTED`.

        ## Request Body

        **Required**

        `list_item_id` (`number`): ID for the Facility List Item rejecting a
                       match with an existing Facility.

        `facility_match_id` (`number`): ID for the potential Facility Match
                            rejected as a match for the Facility List Item.

        **Example**

            {
                "list_item_id": 1,
                "facility_match_id": 1
            }

        ## Sample Response

            {
                "id": 1,
                "matches": [
                    {
                        "id": 1,
                        "status": "CONFIRMED",
                        "confidence": 0.6,
                        "results": {
                            "match_type": "single_gazetteer_match",
                            "code_version": "12asdf",
                            "recall_weight": 1,
                            "automatic_threshold": 0.8,
                            "gazetteer_threshold": 0.5,
                            "no_gazetteer_matches": false
                        }
                        "oar_id": "oar_id_1",
                        "name": "facility match name 1",
                        "address": "facility match address 1",
                        "location": {
                            "lat": 1,
                            "lng": 1
                        }
                    },
                    {
                        "id": 2,
                        "status": "REJECTED",
                        "confidence": 0.7,
                        "results": {
                            "match_type": "single_gazetteer_match",
                            "code_version": "34asdf",
                            "recall_weight": 1,
                            "automatic_threshold": 0.8,
                            "gazetteer_threshold": 0.5,
                            "no_gazetteer_matches": false
                        }
                        "oar_id": "oar_id_2",
                        "name": "facility match name 2",
                        "address": "facility match address 2",
                        "location": {
                            "lat": 2,
                            "lng": 2
                        }
                    }
                ],
                "row_index": 1,
                "address": "facility list item address",
                "name": "facility list item name",
                "raw_data": "facility liste item name, facility list item address", # noqa
                "status": "CONFIRMED_MATCH",
                "processing_started_at": null,
                "processing_completed_at": null,
                "country_code": "US",
                "facility_list": 1,
                "country_name": "United States",
                "processing_errors": null,
                "list_statuses": ["CONFIRMED_MATCH"],
                "matched_facility": {
                    "oar_id": "oar_id_1",
                    "name": "facility match name 1",
                    "address": "facility match address 1",
                    "location": {
                        "lat": 1,
                        "lng": 1
                    },
                    "created_from_id": 12345
                }
            }
        """
        try:
            list_item_id = request.data.get('list_item_id')
            facility_match_id = request.data.get('facility_match_id')

            if list_item_id is None:
                raise ValidationError('missing required list_item_id')

            if facility_match_id is None:
                raise ValidationError('missing required facility_match_id')

            user_contributor = request.user.contributor
            facility_list = FacilityList \
                .objects \
                .filter(contributor=user_contributor) \
                .get(pk=pk)
            facility_list_item = FacilityListItem \
                .objects \
                .filter(facility_list=facility_list) \
                .get(pk=list_item_id)
            facility_match = FacilityMatch \
                .objects \
                .filter(facility_list_item=facility_list_item) \
                .get(pk=facility_match_id)

            if facility_list_item.status != FacilityListItem.POTENTIAL_MATCH:
                raise ValidationError(
                    'facility list item status must be POTENTIAL_MATCH')

            if facility_match.status != FacilityMatch.PENDING:
                raise ValidationError('facility match status must be PENDING')

            facility_match.status = FacilityMatch.CONFIRMED
            facility_match.save()

            FacilityMatch \
                .objects \
                .filter(facility_list_item=facility_list_item) \
                .exclude(pk=facility_match_id) \
                .update(status=FacilityMatch.REJECTED)

            facility_list_item.status = FacilityListItem.CONFIRMED_MATCH
            facility_list_item.facility = facility_match.facility
            facility_list_item.save()

            response_data = FacilityListItemSerializer(facility_list_item).data

            response_data['list_statuses'] = (facility_list
                                              .facilitylistitem_set
                                              .values_list('status', flat=True)
                                              .distinct())

            return Response(response_data)
        except FacilityList.DoesNotExist:
            raise NotFound()
        except FacilityListItem.DoesNotExist:
            raise NotFound()
        except FacilityMatch.DoesNotExist:
            raise NotFound()

    @transaction.atomic
    @action(detail=True,
            methods=['post'],
            url_path='reject')
    def reject_match(self, request, pk=None):
        """
        Reject a potential match between an existing Facility and a Facility
        List Item from an authenticated Contributor's Facility List.

        Returns an updated Facility List Item with the potential match's status
        changed to `REJECTED`.

        If all potential matches have been rejected and the Facility List Item
        has been successfully geocoded, creates a new Facility from the
        Facility List Item.

        ## Request Body

        **Required**

        `list_item_id` (`number`): ID for the Facility List Item rejecting a
                       match with an existing Facility.

        `facility_match_id` (`number`): ID for the potential Facility Match
                            rejected as a match for the Facility List Item.

        **Example**

            {
                "list_item_id": 1,
                "facility_match_id": 2
            }

        ## Sample Response

            {
                "id": 1,
                "matches": [
                    {
                        "id": 1,
                        "status": "PENDING",
                        "confidence": 0.6,
                        "results": {
                            "match_type": "single_gazetteer_match",
                            "code_version": "12asdf",
                            "recall_weight": 1,
                            "automatic_threshold": 0.8,
                            "gazetteer_threshold": 0.5,
                            "no_gazetteer_matches": false
                        }
                        "oar_id": "oar_id_1",
                        "name": "facility match name 1",
                        "address": "facility match address 1",
                        "location": {
                            "lat": 1,
                            "lng": 1
                        }
                    },
                    {
                        "id": 2,
                        "status": "REJECTED",
                        "confidence": 0.7,
                        "results": {
                            "match_type": "single_gazetteer_match",
                            "code_version": "34asdf",
                            "recall_weight": 1,
                            "automatic_threshold": 0.8,
                            "gazetteer_threshold": 0.5,
                            "no_gazetteer_matches": false
                        }
                        "oar_id": "oar_id_2",
                        "name": "facility match name 2",
                        "address": "facility match address 2",
                        "location": {
                            "lat": 2,
                            "lng": 2
                        }
                    }
                ]
                "row_index": 1,
                "address": "facility list item address",
                "name": "facility list item name",
                "raw_data": "facility liste item name, facility list item address", # noqa
                "status": "POTENTIAL_MATCH",
                "processing_started_at": null,
                "processing_completed_at": null,
                "country_code": "US",
                "country_name": "United States",
                "matched_facility": null,
                "processing_errors": null,
                "facility_list": 1,
                "list_statuses": ["POTENTIAL_MATCH"],
            }
        """
        try:
            list_item_id = request.data.get('list_item_id')
            facility_match_id = request.data.get('facility_match_id')

            if list_item_id is None:
                raise ValidationError('missing required list_item_id')

            if facility_match_id is None:
                raise ValidationError('missing required facility_match_id')

            user_contributor = request.user.contributor
            facility_list = FacilityList \
                .objects \
                .filter(contributor=user_contributor) \
                .get(pk=pk)
            facility_list_item = FacilityListItem \
                .objects \
                .filter(facility_list=facility_list) \
                .get(pk=list_item_id)
            facility_match = FacilityMatch \
                .objects \
                .filter(facility_list_item=facility_list_item) \
                .get(pk=facility_match_id)

            if facility_list_item.status != FacilityListItem.POTENTIAL_MATCH:
                raise ValidationError(
                    'facility list item status must be POTENTIAL_MATCH')

            if facility_match.status != FacilityMatch.PENDING:
                raise ValidationError('facility match status must be PENDING')

            facility_match.status = FacilityMatch.REJECTED
            facility_match.save()

            remaining_potential_matches = FacilityMatch \
                .objects \
                .filter(facility_list_item=facility_list_item) \
                .filter(status=FacilityMatch.PENDING)

            # If no potential matches remain:
            #
            # - create a new facility for a list item with a geocoded point
            # - set status to `ERROR_MATCHING` for list item with no point
            if remaining_potential_matches.count() == 0:
                no_location = facility_list_item.geocoded_point is None
                no_geocoding_results = facility_list_item.status == \
                    FacilityListItem.GEOCODED_NO_RESULTS

                if (no_location or no_geocoding_results):
                    facility_list_item.status = FacilityListItem.ERROR_MATCHING
                    timestamp = str(datetime.utcnow())
                    facility_list_item.processing_results.append({
                        'action': ProcessingAction.CONFIRM,
                        'started_at': timestamp,
                        'error': True,
                        'message': ('Unable to create a new facility from an '
                                    'item with no geocoded location'),
                        'finished_at': timestamp,
                    })
                else:
                    new_facility = Facility \
                        .objects \
                        .create(name=facility_list_item.name,
                                address=facility_list_item.address,
                                country_code=facility_list_item.country_code,
                                location=facility_list_item.geocoded_point,
                                created_from=facility_list_item)

                    # also create a new facility match
                    match_results = {
                        "match_type": "all_potential_matches_rejected",
                    }

                    FacilityMatch \
                        .objects \
                        .create(facility_list_item=facility_list_item,
                                facility=new_facility,
                                confidence=1.0,
                                status=FacilityMatch.CONFIRMED,
                                results=match_results)

                    facility_list_item.facility = new_facility

                    facility_list_item.status = FacilityListItem \
                        .CONFIRMED_MATCH

                facility_list_item.save()

            response_data = FacilityListItemSerializer(facility_list_item).data

            response_data['list_statuses'] = (facility_list
                                              .facilitylistitem_set
                                              .values_list('status', flat=True)
                                              .distinct())

            return Response(response_data)
        except FacilityList.DoesNotExist:
            raise NotFound()
        except FacilityListItem.DoesNotExist:
            raise NotFound()
        except FacilityMatch.DoesNotExist:
            raise NotFound()
